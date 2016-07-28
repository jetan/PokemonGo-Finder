#!/usr/bin/python
# -*- coding: utf-8 -*-

import flask
import notifier
from flask import Flask, render_template
from flask_googlemaps import GoogleMaps
from flask_googlemaps import Map
from flask_googlemaps import icons
import os
import re
import sys
import struct
import json
import requests
import argparse
import getpass
import threading
import werkzeug.serving
import pokemon_pb2
import time
import warnings
from google.protobuf.internal import encoder
from google.protobuf.message import DecodeError
from s2sphere import *
from datetime import datetime
from geopy.geocoders import GoogleV3
from gpsoauth import perform_master_login, perform_oauth
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from requests.adapters import ConnectionError
from requests.models import InvalidURL
from transform import *

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

API_URL = 'https://pgorelease.nianticlabs.com/plfe/rpc'
LOGIN_URL = \
    'https://sso.pokemon.com/sso/login?service=https://sso.pokemon.com/sso/oauth2.0/callbackAuthorize'
LOGIN_OAUTH = 'https://sso.pokemon.com/sso/oauth2.0/accessToken'
APP = 'com.nianticlabs.pokemongo'

with open('credentials.json') as file:
    credentials = json.load(file)

PTC_CLIENT_SECRET = credentials.get('ptc_client_secret', None)
ANDROID_ID = credentials.get('android_id', None)
SERVICE = credentials.get('service', None)
CLIENT_SIG = credentials.get('client_sig', None)
GOOGLEMAPS_KEY = credentials.get('gmaps_key', None)

SESSION = requests.session()
SESSION.headers.update({'User-Agent': 'Niantic App'})
SESSION.verify = False

global_password = None
global_token = None
DEBUG = True
VERBOSE_DEBUG = False  # if you want to write raw request/response to the console
COORDS_LATITUDE = 0
COORDS_LONGITUDE = 0
COORDS_ALTITUDE = 0
FLOAT_LAT = 0
FLOAT_LONG = 0
NEXT_LAT = 0
NEXT_LONG = 0
auto_refresh = 0
steplimit = None
show_scan_locations = False
current_scan_location = 0
relog_time = 0
default_step = 0.001
pokemons = {}
gyms = {}
pokestops = {}
numbertoteam = {  # At least I'm pretty sure that's it. I could be wrong and then I'd be displaying the wrong owner team of gyms.
    0: 'Gym',
    1: 'Mystic',
    2: 'Valor',
    3: 'Instinct',
}
origin_lat, origin_lon = None, None
is_ampm_clock = False

# stuff for in-background search thread
search_thread = None

# Login session
login_session = None

def memoize(obj):
    cache = obj.cache = {}

    @functools.wraps(obj)
    def memoizer(*args, **kwargs):
        key = str(args) + str(kwargs)
        if key not in cache:
            cache[key] = obj(*args, **kwargs)
        return cache[key]
    return memoizer

def parse_unicode(bytestring):
    decoded_string = bytestring.decode(sys.getfilesystemencoding())
    return decoded_string


def debug(message):
    if DEBUG:
        print '[-] {}'.format(message)


def time_left(ms):
    s = ms / 1000
    (m, s) = divmod(s, 60)
    (h, m) = divmod(m, 60)
    return (h, m, s)


def encode(cellid):
    output = []
    encoder._VarintEncoder()(output.append, cellid)
    return ''.join(output)


def getNeighbors():
    origin = CellId.from_lat_lng(LatLng.from_degrees(FLOAT_LAT,
                                                     FLOAT_LONG)).parent(15)
    walk = [origin.id()]

    # 10 before and 10 after

    next = origin.next()
    prev = origin.prev()
    for i in range(10):
        walk.append(prev.id())
        walk.append(next.id())
        next = next.next()
        prev = prev.prev()
    return walk


def f2i(float):
    return struct.unpack('<Q', struct.pack('<d', float))[0]


def f2h(float):
    return hex(struct.unpack('<Q', struct.pack('<d', float))[0])


def h2f(hex):
    return struct.unpack('<d', struct.pack('<Q', int(hex, 16)))[0]


def retrying_set_location(location_name):
    """
    Continue trying to get co-ords from Google Location until we have them
    :param location_name: string to pass to Location API
    :return: None
    """

    while True:
        try:
            set_location(location_name)
            return
        except (GeocoderTimedOut, GeocoderServiceError), e:
            debug(
                'retrying_set_location: geocoder exception ({}), retrying'.format(
                    str(e)))
        time.sleep(1.25)


def set_location(location_name):
    geolocator = GoogleV3()
    prog = re.compile('^(\-?\d+(\.\d+)?),\s*(\-?\d+(\.\d+)?)$')
    global origin_lat
    global origin_lon
    if prog.match(location_name):
        local_lat, local_lng = [float(x) for x in location_name.split(",")]
        alt = 0
        origin_lat, origin_lon = local_lat, local_lng
    else:
        loc = geolocator.geocode(location_name)
        origin_lat, origin_lon = local_lat, local_lng = loc.latitude, loc.longitude
        alt = loc.altitude
        print '[!] Your given location: {}'.format(loc.address.encode('utf-8'))

    print('[!] lat/long/alt: {} {} {}'.format(local_lat, local_lng, alt))
    set_location_coords(local_lat, local_lng, alt)


def set_location_coords(lat, long, alt):
    global COORDS_LATITUDE, COORDS_LONGITUDE, COORDS_ALTITUDE
    global FLOAT_LAT, FLOAT_LONG
    FLOAT_LAT = lat
    FLOAT_LONG = long
    COORDS_LATITUDE = f2i(lat)  # 0x4042bd7c00000000 # f2i(lat)
    COORDS_LONGITUDE = f2i(long)  # 0xc05e8aae40000000 #f2i(long)
    COORDS_ALTITUDE = f2i(alt)


def get_location_coords():
    return (COORDS_LATITUDE, COORDS_LONGITUDE, COORDS_ALTITUDE)


def retrying_api_req(service, api_endpoint, access_token, *args, **kwargs):
    while True:
        try:
            response = api_req(service, api_endpoint, access_token, *args,
                               **kwargs)
            if response:
                return response
            debug('retrying_api_req: api_req returned None, retrying')
        except (InvalidURL, ConnectionError, DecodeError), e:
            debug('retrying_api_req: request error ({}), retrying'.format(
                str(e)))
        time.sleep(1)


def api_req(service, api_endpoint, access_token, *args, **kwargs):
    p_req = pokemon_pb2.RequestEnvelop()
    p_req.rpc_id = 1469378659230941192

    p_req.unknown1 = 2

    (p_req.latitude, p_req.longitude, p_req.altitude) = \
        get_location_coords()

    p_req.unknown12 = 989

    if 'useauth' not in kwargs or not kwargs['useauth']:
        p_req.auth.provider = service
        p_req.auth.token.contents = access_token
        p_req.auth.token.unknown13 = 14
    else:
        p_req.unknown11.unknown71 = kwargs['useauth'].unknown71
        p_req.unknown11.unknown72 = kwargs['useauth'].unknown72
        p_req.unknown11.unknown73 = kwargs['useauth'].unknown73

    for arg in args:
        p_req.MergeFrom(arg)

    protobuf = p_req.SerializeToString()

    r = SESSION.post(api_endpoint, data=protobuf, verify=False)

    p_ret = pokemon_pb2.ResponseEnvelop()
    p_ret.ParseFromString(r.content)

    if VERBOSE_DEBUG:
        print 'REQUEST:'
        print p_req
        print 'Response:'
        print p_ret
        print '''
'''
    time.sleep(0.51)
    return p_ret


def get_api_endpoint(service, access_token, api=API_URL):
    profile_response = None
    while not profile_response:
        profile_response = retrying_get_profile(service, access_token, api,
                                                None)
        if not hasattr(profile_response, 'api_url'):
            debug(
                'retrying_get_profile: get_profile returned no api_url, retrying')
            profile_response = None
            continue
        if not len(profile_response.api_url):
            debug(
                'get_api_endpoint: retrying_get_profile returned no-len api_url, retrying')
            profile_response = None

    return 'https://%s/rpc' % profile_response.api_url

def retrying_get_profile(service, access_token, api, useauth, *reqq):
    profile_response = None
    while not profile_response:
        profile_response = get_profile(service, access_token, api, useauth,
                                       *reqq)
        if not hasattr(profile_response, 'payload'):
            debug(
                'retrying_get_profile: get_profile returned no payload, retrying')
            profile_response = None
            continue
        if not profile_response.payload:
            debug(
                'retrying_get_profile: get_profile returned no-len payload, retrying')
            profile_response = None

    return profile_response

def get_profile(service, access_token, api, useauth, *reqq):
    req = pokemon_pb2.RequestEnvelop()
    req1 = req.requests.add()
    req1.type = 2
    if len(reqq) >= 1:
        req1.MergeFrom(reqq[0])

    req2 = req.requests.add()
    req2.type = 126
    if len(reqq) >= 2:
        req2.MergeFrom(reqq[1])

    req3 = req.requests.add()
    req3.type = 4
    if len(reqq) >= 3:
        req3.MergeFrom(reqq[2])

    req4 = req.requests.add()
    req4.type = 129
    if len(reqq) >= 4:
        req4.MergeFrom(reqq[3])

    req5 = req.requests.add()
    req5.type = 5
    if len(reqq) >= 5:
        req5.MergeFrom(reqq[4])
    return retrying_api_req(service, api, access_token, req, useauth=useauth)

def login_google(username, password):
    print '[!] Google login for: {}'.format(username)
    r1 = perform_master_login(username, password, ANDROID_ID)
    r2 = perform_oauth(username,
                       r1.get('Token', ''),
                       ANDROID_ID,
                       SERVICE,
                       APP,
                       CLIENT_SIG, )
    return r2.get('Auth')

def login_ptc(username, password):
    print '[!] PTC login for: {}'.format(username)
    head = {'User-Agent': 'Niantic App'}
    r = SESSION.get(LOGIN_URL, headers=head)
    if r is None:
        return render_template('nope.html', fullmap=fullmap)

    try:
        jdata = json.loads(r.content)
    except ValueError, e:
        debug('login_ptc: could not decode JSON from {}'.format(r.content))
        return None

    # Maximum password length is 15 (sign in page enforces this limit, API does not)

    if len(password) > 15:
        print '[!] Trimming password to 15 characters'
        password = password[:15]

    data = {
        'lt': jdata['lt'],
        'execution': jdata['execution'],
        '_eventId': 'submit',
        'username': username,
        'password': password,
    }
    r1 = SESSION.post(LOGIN_URL, data=data, headers=head)

    ticket = None
    try:
        ticket = re.sub('.*ticket=', '', r1.history[0].headers['Location'])
    except Exception, e:
        if DEBUG:
            print r1.json()['errors'][0]
        return None

    data1 = {
        'client_id': 'mobile-app_pokemon-go',
        'redirect_uri': 'https://www.nianticlabs.com/pokemongo/error',
        'client_secret': PTC_CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'code': ticket,
    }
    r2 = SESSION.post(LOGIN_OAUTH, data=data1)
    access_token = re.sub('&expires.*', '', r2.content)
    access_token = re.sub('.*access_token=', '', access_token)

    return access_token


def get_heartbeat(service,
                  api_endpoint,
                  access_token,
                  response, ):
    m4 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleInt()
    m.f1 = int(time.time() * 1000)
    m4.message = m.SerializeToString()
    m5 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleString()
    m.bytes = '05daf51635c82611d1aac95c0b051d3ec088a930'
    m5.message = m.SerializeToString()
    walk = sorted(getNeighbors())
    m1 = pokemon_pb2.RequestEnvelop.Requests()
    m1.type = 106
    m = pokemon_pb2.RequestEnvelop.MessageQuad()
    m.f1 = ''.join(map(encode, walk))
    m.f2 = \
        "\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000"
    m.lat = COORDS_LATITUDE
    m.long = COORDS_LONGITUDE
    m1.message = m.SerializeToString()
    response = get_profile(service,
                           access_token,
                           api_endpoint,
                           response.unknown7,
                           m1,
                           pokemon_pb2.RequestEnvelop.Requests(),
                           m4,
                           pokemon_pb2.RequestEnvelop.Requests(),
                           m5, )

    try:
        payload = response.payload[0]
    except (AttributeError, IndexError):
        return

    heartbeat = pokemon_pb2.ResponseEnvelop.HeartbeatPayload()
    heartbeat.ParseFromString(payload)
    return heartbeat

def get_token(service, username, password):
    """
    Get token if it's not None
    :return:
    :rtype:
    """

    global global_token
    if global_token is None:
        if service == 'ptc':
            global_token = login_ptc(username, password)
        else:
            global_token = login_google(username, password)
        return global_token
    else:
        return global_token

def get_args():
    # load default args
    default_args = {
        "DEBUG": True,
        "ampm_clock": False,
        "auth_service": "ptc",
        "auto_refresh": None,
        "china": False,
        "debug": False,
        "display_gym": False,
        "display_pokestop": False,
        "do_not_notify": None,
        "host": "127.0.0.1",
        "ignore": None,
        "locale": "en",
        "location": None,
        "notify": None,
        "only": None,
        "onlylure": False,
        "password": None,
        "port": 5000,
        "pushbullet": None,
        "step_limit": 4,
        "username": None,
        "show_scan_locations": False
    }

    INTEGER_STR = "int"
    BOOLEAN_STR = "bool"
    STRING_STR = "str"
    default_args_type = {
        "DEBUG": BOOLEAN_STR,
        "ampm_clock": BOOLEAN_STR,
        "auth_service": STRING_STR,
        "auto_refresh": INTEGER_STR,
        "china": BOOLEAN_STR,
        "debug": BOOLEAN_STR,
        "display_gym": BOOLEAN_STR,
        "display_pokestop": BOOLEAN_STR,
        "do_not_notify": STRING_STR,
        "host": STRING_STR,
        "ignore": STRING_STR,
        "locale": STRING_STR,
        "location": STRING_STR,
        "notify": STRING_STR,
        "only": STRING_STR,
        "onlylure": BOOLEAN_STR,
        "password": STRING_STR,
        "port": INTEGER_STR,
        "pushbullet": STRING_STR,
        "step_limit": INTEGER_STR,
        "username": STRING_STR,
        "show_scan_locations": BOOLEAN_STR
    }
    # load config file
    with open('config.json') as data_file:
        data = json.load(data_file)
        for key in data:
            if key not in default_args_type:
                warnings.warn( 'Config Item ' + key + 'Does Not Have a Default Type' )

            if default_args_type[key] == INTEGER_STR:
                default_args[key] = int(data[key])

            elif default_args_type[key] == BOOLEAN_STR:
                default_args[key] = data[key]

            else:
                if default_args_type[key] != STRING_STR:
                    warnings.warn( 'Unsupported Default Args Type' )

                default_args[key] = str(data[key])
        # create namespace obj
        namespace = argparse.Namespace()
        for key in default_args:
            vars(namespace)[key] = default_args[key]
        return namespace

def login(args):
    global login_session
    if login_session:
        return login_session

    global global_password
    if not global_password:
      if args.password:
        global_password = args.password
      else:
        global_password = getpass.getpass()

    access_token = get_token(args.auth_service, args.username, global_password)
    if access_token is None:
        raise Exception('[-] Wrong username/password')

    print '[+] RPC Session Token: {} ...'.format(access_token[:25])

    api_endpoint = get_api_endpoint(args.auth_service, access_token)
    if api_endpoint is None:
        raise Exception('[-] RPC server offline')

    print '[+] Received API endpoint: {}'.format(api_endpoint)

    profile_response = retrying_get_profile(args.auth_service, access_token,
                                            api_endpoint, None)
    if profile_response is None or not profile_response.payload:
        raise Exception('Could not get profile')

    print '[+] Login successful'

    payload = profile_response.payload[0]
    profile = pokemon_pb2.ResponseEnvelop.ProfilePayload()
    profile.ParseFromString(payload)
    print '[+] Username: {}'.format(profile.profile.username)

    creation_time = \
        datetime.fromtimestamp(int(profile.profile.creation_time)
                               / 1000)
    print '[+] You started playing Pokemon Go on: {}'.format(
        creation_time.strftime('%Y-%m-%d %H:%M:%S'))

    for curr in profile.profile.currency:
        print '[+] {}: {}'.format(curr.type, curr.amount)

    login_session = api_endpoint, access_token, profile_response
    return login_session

def handle_push(json_obj):
    location = ''
    if 'push' in json_obj and json_obj['push']['type'] == 'mirror':
        if json_obj['push']['title'] == 'pogo-find: Location':
            location = json_obj['push']['body'].rstrip()
            retrying_set_location(location)
            print "[+] Changed location: " + location
            notifier.send_note("Location updated!", location)
        elif json_obj['push']['title'] == 'pogo-find: Distance':
            global steplimit
            steplimit = int(json_obj['push']['body'])
            print "[+] Changed step limit: " + str(steplimit)
            notifier.send_note("Step limit updated!", str(steplimit))

def main():
    full_path = os.path.realpath(__file__)
    (path, filename) = os.path.split(full_path)

    args = get_args()

    if args.auth_service not in ['ptc', 'google']:
        print '[!] Invalid Auth service specified'
        return

    print('[+] Locale is ' + args.locale)
    pokemonsJSON = json.load(
        open(path + '/locales/pokemon.' + args.locale + '.json'))

    if args.debug:
        global DEBUG
        DEBUG = True
        print '[!] DEBUG mode on'

    global show_scan_locations
    if args.show_scan_locations:
        show_scan_locations = True

    # only get location for first run
    if not (FLOAT_LAT and FLOAT_LONG):
      print('[+] Getting initial location')
      retrying_set_location(args.location)

    if args.auto_refresh:
        global auto_refresh
        auto_refresh = int(args.auto_refresh) * 1000

    if args.ampm_clock:
        global is_ampm_clock
        is_ampm_clock = True

    global relog_time
    if time.time() > relog_time:
        relog_time = time.time() + 600
        print "[+] Logging in to Pokemon Go"
        global login_session, global_token
        login_session = None
        global_token = None

    api_endpoint, access_token, profile_response = login(args)

    clear_stale_pokemons()

    notifier.start_listener(handle_push)

    global steplimit
    if not steplimit:
        steplimit = int(args.step_limit)

    ignore = []
    only = []
    if args.ignore:
        ignore = [i.lower().strip() for i in args.ignore.split(',')]
    elif args.only:
        only = [i.lower().strip() for i in args.only.split(',')]

    global current_scan_location
    pos = 1
    x = 0
    y = 0
    dx = 0
    dy = -1
    steplimit2 = steplimit**2
    for step in range(steplimit2):
        current_scan_location = step + 1
        #starting at 0 index
        debug('looping: step {} of {}'.format((step+1), steplimit**2))
        #debug('steplimit: {} x: {} y: {} pos: {} dx: {} dy {}'.format(steplimit2, x, y, pos, dx, dy))
        # Scan location math
        if -steplimit2 / 2 < x <= steplimit2 / 2 and -steplimit2 / 2 < y <= steplimit2 / 2:
            set_location_coords(x * 0.0025 + origin_lat, y * 0.0030 + origin_lon, 0)
        if x == y or x < 0 and x == -y or x > 0 and x == 1 - y:
            (dx, dy) = (-dy, dx)

        (x, y) = (x + dx, y + dy)

        process_step(args, api_endpoint, access_token, profile_response,
                     pokemonsJSON, ignore, only)

        print('Completed: ' + str(
            ((step+1) + pos * .25 - .25) / (steplimit2) * 100) + '%')

    current_scan_location = 0

    global NEXT_LAT, NEXT_LONG
    if (NEXT_LAT and NEXT_LONG and
            (NEXT_LAT != FLOAT_LAT or NEXT_LONG != FLOAT_LONG)):
        print('Update to next location %f, %f' % (NEXT_LAT, NEXT_LONG))
        set_location_coords(NEXT_LAT, NEXT_LONG, 0)
        NEXT_LAT = 0
        NEXT_LONG = 0
    else:
        set_location_coords(origin_lat, origin_lon, 0)

    register_background_thread()

def process_step(args, api_endpoint, access_token, profile_response,
                 pokemonsJSON, ignore, only):
    print('[+] Searching for Pokemon at location {} {}'.format(FLOAT_LAT, FLOAT_LONG))
    origin = LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)
    step_lat = FLOAT_LAT
    step_long = FLOAT_LONG
    parent = CellId.from_lat_lng(LatLng.from_degrees(FLOAT_LAT,
                                                     FLOAT_LONG)).parent(15)
    h = get_heartbeat(args.auth_service, api_endpoint, access_token,
                      profile_response)
    hs = [h]
    seen = {}

    for child in parent.children():
        latlng = LatLng.from_point(Cell(child).get_center())
        set_location_coords(latlng.lat().degrees, latlng.lng().degrees, 0)
        hs.append(
            get_heartbeat(args.auth_service, api_endpoint, access_token,
                          profile_response))
    set_location_coords(step_lat, step_long, 0)
    visible = []

    for hh in hs:
        try:
            for cell in hh.cells:
                for wild in cell.WildPokemon:
                    hash = wild.SpawnPointId;
                    if hash not in seen.keys() or (seen[hash].TimeTillHiddenMs <= wild.TimeTillHiddenMs):
                        visible.append(wild)
                    seen[hash] = wild.TimeTillHiddenMs
                if cell.Fort:
                    for Fort in cell.Fort:
                        if Fort.Enabled == True:
                            if args.china:
                                (Fort.Latitude, Fort.Longitude) = \
transform_from_wgs_to_gcj(Location(Fort.Latitude, Fort.Longitude))
                            if Fort.GymPoints and args.display_gym:
                                gyms[Fort.FortId] = [Fort.Team, Fort.Latitude,
                                                     Fort.Longitude, Fort.GymPoints]

                            elif Fort.FortType \
                                and args.display_pokestop:
                                expire_time = 0
                                if Fort.LureInfo.LureExpiresTimestampMs:
                                    expire_time = datetime\
                                        .fromtimestamp(Fort.LureInfo.LureExpiresTimestampMs / 1000.0)\
                                        .strftime("%H:%M:%S")
                                if (expire_time != 0 or not args.onlylure):
                                    pokestops[Fort.FortId] = [Fort.Latitude,
                                                              Fort.Longitude, expire_time]
        except AttributeError:
            # Reset login session if problems happen
            global login_session, global_token
            login_session = None
            global_token = None
            break

    for poke in visible:
        pokeid = str(poke.pokemon.PokemonId)
        pokename = pokemonsJSON[pokeid]
        if args.ignore:
            if pokename.lower() in ignore or pokeid in ignore:
                continue
        elif args.only:
            if pokename.lower() not in only and pokeid not in only:
                continue

        error_text1 = ''
        error_text2 = ''
        error_br = ''
        if poke.TimeTillHiddenMs <= 0:
            print "[+] Found error time, setting to 15 minutes: " + str(poke.TimeTillHiddenMs)
            poke.TimeTillHiddenMs = 900000
            error_text1 = '(!) '
            error_text2 = 'Error: disappear_time was invalid, set to 15 minutes instead.'
            error_br = '<br>'
        disappear_timestamp = time.time() + poke.TimeTillHiddenMs \
            / 1000

        if args.china:
            (poke.Latitude, poke.Longitude) = \
                transform_from_wgs_to_gcj(Location(poke.Latitude,
                    poke.Longitude))

        datestr = datetime.fromtimestamp(disappear_timestamp)
        dateoutput = datestr.strftime("%H:%M:%S")
        if is_ampm_clock:
            dateoutput = datestr.strftime("%I:%M:%S%p").lstrip('0')

        pokemon_obj = {
            "lat": poke.Latitude,
            "lng": poke.Longitude,
            "disappear_time": disappear_timestamp,
            "id": poke.pokemon.PokemonId,
            "name": pokename,
            "error_text1": error_text1,
            "error_text2": error_text2,
            "error_br": error_br,
            "disappear_time_formatted": dateoutput
        }

        pokespawnkey = poke.SpawnPointId + pokename
        if pokespawnkey not in pokemons:
            pushbullet_iden = notifier.pokemon_found(pokemon_obj)
            pokemon_obj['pushbullet_iden'] = pushbullet_iden
            pokemons[pokespawnkey] = pokemon_obj

def clear_stale_pokemons():
    current_time = time.time()

    for pokemon_key in pokemons.keys():
        pokemon = pokemons[pokemon_key]
        if current_time > pokemon['disappear_time']:
            print "[+] removing stale pokemon %s at %f, %f from list" % (
                pokemon['name'].encode('utf-8'), pokemon['lat'], pokemon['lng'])
            notifier.pokemon_expired(pokemon)
            del pokemons[pokemon_key]


def register_background_thread(initial_registration=False):
    """
    Start a background thread to search for Pokemon
    while Flask is still able to serve requests for the map
    :param initial_registration: True if first registration and thread should start immediately, False if it's being called by the finishing thread to schedule a refresh
    :return: None
    """

    debug('register_background_thread called')
    global search_thread

    if initial_registration:
        if not werkzeug.serving.is_running_from_reloader():
            debug(
                'register_background_thread: not running inside Flask so not starting thread')
            return
        if search_thread:
            debug(
                'register_background_thread: initial registration requested but thread already running')
            return

        debug('register_background_thread: initial registration')
        search_thread = threading.Thread(target=main)

    else:
        debug('register_background_thread: queueing')
        search_thread = threading.Timer(30, main)  # delay, in seconds

    search_thread.daemon = True
    search_thread.name = 'search_thread'
    search_thread.start()


def create_app():
    app = Flask(__name__, template_folder='templates')

    GoogleMaps(app, key=GOOGLEMAPS_KEY)
    return app


app = create_app()


@app.route('/data')
def data():
    """ Gets all the PokeMarkers via REST """
    return json.dumps(get_pokemarkers())

@app.route('/raw_data')
def raw_data():
    """ Gets raw data for pokemons/gyms/pokestops via REST """
    return flask.jsonify(pokemons=pokemons, gyms=gyms, pokestops=pokestops)


@app.route('/config')
def config():
    """ Gets the settings for the Google Maps via REST"""
    center = {
        'lat': FLOAT_LAT,
        'lng': FLOAT_LONG,
        'zoom': 15,
        'identifier': "fullmap"
    }
    return json.dumps(center)


@app.route('/')
def fullmap():
    clear_stale_pokemons()

    return render_template(
        'example_fullmap.html', key=GOOGLEMAPS_KEY, fullmap=get_map(), auto_refresh=auto_refresh)


@app.route('/next_loc')
def next_loc():
    global NEXT_LAT, NEXT_LONG

    lat = flask.request.args.get('lat', '')
    lon = flask.request.args.get('lon', '')
    if not (lat and lon):
        print('[-] Invalid next location: %s,%s' % (lat, lon))
    else:
        print('[+] Saved next location as %s,%s' % (lat, lon))
        NEXT_LAT = float(lat)
        NEXT_LONG = float(lon)
        return 'ok'

@app.route('/new_loc')
def new_loc():
    global origin_lat, origin_lon

    lat = flask.request.args.get('lat', '')
    lon = flask.request.args.get('lon', '')
    if not (lat and lon):
        print('[-] Invalid new location: %s,%s' % (lat, lon))
    else:
        print('[+] Saved new location as %s,%s' % (lat, lon))
        origin_lat = float(lat)
        origin_lon = float(lon)
        return 'ok'

def get_pokemarkers():
    pokeMarkers = [{
        'icon': icons.dots.red,
        'lat': origin_lat,
        'lng': origin_lon,
        'infobox': "Start position",
        'type': 'custom',
        'key': 'start-position',
        'disappear_time': -1,
        'draggable': True
    }]

    global steplimit
    if show_scan_locations and steplimit:
        global current_scan_location
        pos = 1
        x = 0
        y = 0
        dx = 0
        dy = -1
        steplimit2 = steplimit**2
        for step in range(steplimit2):
            this_icon = icons.dots.blue
            icon_refresher = 0
            if current_scan_location == step:
                this_icon = icons.dots.pink
                icon_refresher = 0.00000001
            # Scan location math
            if step > 0 and -steplimit2 / 2 < x <= steplimit2 / 2 and -steplimit2 / 2 < y <= steplimit2 / 2:
                pokeMarkers.append({
                    'icon': this_icon,
                    'lat': x * 0.0025 + origin_lat + icon_refresher,
                    'lng': y * 0.0030 + origin_lon,
                    'infobox': "Scan position " + str(step+1),
                    'type': 'custom',
                    'key': 'scan-position-' + str(step+1),
                    'disappear_time': -1
                })
            if x == y or x < 0 and x == -y or x > 0 and x == 1 - y:
                (dx, dy) = (-dy, dx)
            (x, y) = (x + dx, y + dy)

    for pokemon_key in pokemons:
        pokemon = pokemons[pokemon_key]

        LABEL_TMPL = u'''
<div><b>{error_text1}{name}</b><span> - </span><small><a href='http://www.pokemon.com/us/pokedex/{id}' target='_blank' title='View in Pokedex'>#{id}</a></small></div>
<div>{error_text2}{error_br}Disappears at - {disappear_time_formatted} <span class='label-countdown' disappears-at='{disappear_time}'></span></div>
<div><a href='https://www.google.com/maps/dir/Current+Location/{lat},{lng}' target='_blank' title='View in Maps'>Get Directions</a></div>
'''
        label = LABEL_TMPL.format(**pokemon)
        #  NOTE: `infobox` field doesn't render multiple line string in frontend
        label = label.replace('\n', '')

        pokeMarkers.append({
            'type': 'pokemon',
            'key': pokemon_key,
            'disappear_time': pokemon['disappear_time'],
            'icon': 'static/icons/%d.png' % pokemon["id"],
            'lat': pokemon["lat"],
            'lng': pokemon["lng"],
            'infobox': label
        })

    for gym_key in gyms:
        gym = gyms[gym_key]
        if gym[0] == 0:
            color = "rgba(0,0,0,.4)"
        if gym[0] == 1:
            color = "rgba(74, 138, 202, .6)"
        if gym[0] == 2:
            color = "rgba(240, 68, 58, .6)"
        if gym[0] == 3:
            color = "rgba(254, 217, 40, .6)"

        icon = 'static/forts/'+numbertoteam[gym[0]]+'_large.png'
        pokeMarkers.append({
            'icon': 'static/forts/' + numbertoteam[gym[0]] + '.png',
            'type': 'gym',
            'key': gym_key,
            'disappear_time': -1,
            'lat': gym[1],
            'lng': gym[2],
            'infobox': "<div><center><small>Gym owned by:</small><br><b style='color:" + color + "'>Team " + numbertoteam[gym[0]] + "</b><br><img id='" + numbertoteam[gym[0]] + "' height='100px' src='"+icon+"'><br>Prestige: " + str(gym[3]) + "</center>"
        })
    for stop_key in pokestops:
        stop = pokestops[stop_key]
        if stop[2] > 0:
            pokeMarkers.append({
                'type': 'lured_stop',
                'key': stop_key,
                'disappear_time': -1,
                'icon': 'static/forts/PstopLured.png',
                'lat': stop[0],
                'lng': stop[1],
                'infobox': 'Lured Pokestop, expires at ' + stop[2],
            })
        else:
            pokeMarkers.append({
                'type': 'stop',
                'key': stop_key,
                'disappear_time': -1,
                'icon': 'static/forts/Pstop.png',
                'lat': stop[0],
                'lng': stop[1],
                'infobox': 'Pokestop',
            })
    return pokeMarkers


def get_map():
    fullmap = Map(
        identifier="fullmap2",
        style='height:100%;width:100%;top:0;left:0;position:absolute;z-index:200;',
        lat=origin_lat,
        lng=origin_lon,
        markers=get_pokemarkers(),
        zoom='15', )
    return fullmap


if __name__ == '__main__':
    args = get_args()
    register_background_thread(initial_registration=True)
    app.run(debug=True, threaded=True, host=args.host, port=args.port)