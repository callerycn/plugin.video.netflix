# -*- coding: utf-8 -*-
# Author: trummerjo
# Module: MSLHttpRequestHandler
# Created on: 26.01.2017
# License: MIT https://goo.gl/5bMj3H
"""Proxy service to convert manifest and provide license data"""
from __future__ import unicode_literals

import re
import zlib
import json
import time
import base64
import requests

from resources.lib.globals import g
import resources.lib.common as common

from .request_builder import MSLRequestBuilder
from .profiles import enabled_profiles
from .converter import convert_to_dash
from .exceptions import MSLError, LicenseError

CHROME_BASE_URL = 'http://www.netflix.com/api/msl/NFCDCH-LX/cadmium/'
ENDPOINTS = {
    'chrome': {
        'manifest': CHROME_BASE_URL + 'manifest',
        'license': CHROME_BASE_URL + 'license'},
    'edge': {
        'manifest': None,
        'license': None}
}


class MSLHandler(object):
    """Handles session management and crypto for license and manifest
    requests"""
    last_drm_context = ''
    last_playback_context = ''
    session = requests.session()

    def __init__(self):
        # pylint: disable=broad-except
        try:
            msl_data = json.loads(common.load_file('msl_data.json'))
            self.request_builder = MSLRequestBuilder(msl_data)
            common.debug('Loaded MSL data from disk')
        except Exception:
            import traceback
            common.debug(traceback.format_exc())
            common.debug('Stored MSL data expired or not available')
            self.request_builder = MSLRequestBuilder()
            self.perform_key_handshake()
        common.register_slot(
            signal=common.Signals.ESN_CHANGED,
            callback=self.perform_key_handshake)

    def perform_key_handshake(self):
        """Perform a key handshake and initialize crypto keys"""
        if not g.get_esn():
            common.error('Cannot perform key handshake, missing ESN')
            return

        common.debug('Performing key handshake. ESN: {}'.format(g.get_esn()))

        response = _process_json_response(
            self._post(ENDPOINTS['chrome']['manifest'],
                       self.request_builder.handshake_request()))
        headerdata = json.loads(
            base64.standard_b64decode(response['headerdata']))
        self.request_builder.crypto.parse_key_response(headerdata)
        common.debug('Key handshake successful')

    def load_manifest(self, viewable_id):
        """
        Loads the manifets for the given viewable_id and
        returns a mpd-XML-Manifest

        :param viewable_id: The id of of the viewable
        :return: MPD XML Manifest or False if no success
        """
        manifest_request_data = {
            'method': 'manifest',
            'lookupType': 'PREPARE',
            'viewableIds': [viewable_id],
            'profiles': enabled_profiles(),
            'drmSystem': 'widevine',
            'appId': '14673889385265',
            'sessionParams': {
                'pinCapableClient': False,
                'uiplaycontext': 'null'
            },
            'sessionId': '14673889385265',
            'trackId': 0,
            'flavor': 'PRE_FETCH',
            'secureUrls': False,
            'supportPreviewContent': True,
            'forceClearStreams': False,
            'languages': ['de-DE'],
            'clientVersion': '4.0004.899.011',
            'uiVersion': 'akira'
        }
        manifest = self._process_chunked_response(
            self._post(
                ENDPOINTS['chrome']['manifest'],
                self.request_builder.msl_request(manifest_request_data)))
        return self.__tranform_to_dash(manifest)

    def get_license(self, challenge, sid):
        """
        Requests and returns a license for the given challenge and sid
        :param challenge: The base64 encoded challenge
        :param sid: The sid paired to the challengew
        :return: Base64 representation of the licensekey or False unsuccessfull
        """
        license_request_data = {
            'method': 'license',
            'licenseType': 'STANDARD',
            'clientVersion': '4.0004.899.011',
            'uiVersion': 'akira',
            'languages': ['de-DE'],
            'playbackContextId': self.last_playback_context,
            'drmContextIds': [self.last_drm_context],
            'challenges': [{
                'dataBase64': challenge,
                'sessionId': sid
            }],
            'clientTime': int(time.time()),
            'xid': int((int(time.time()) + 0.1612) * 1000)

        }
        response = self._process_chunked_response(
            self._post(
                ENDPOINTS['chrome']['license'],
                self.request_builder.msl_request(license_request_data)))
        if not response['success']:
            common.error('Error getting license: {}'
                         .format(json.dumps(response)))
            raise LicenseError
        return response['result']['licenses'][0]['data']

    def __tranform_to_dash(self, manifest):
        common.save_file('manifest.json', json.dumps(manifest))
        manifest = manifest['result']['viewables'][0]
        self.last_playback_context = manifest['playbackContextId']
        self.last_drm_context = manifest['drmContextId']
        return convert_to_dash(manifest)

    def _post(self, endpoint, request_data):
        """Execute a post request"""
        response = self.session.post(endpoint, request_data)
        response.raise_for_status()
        return response

    def _process_chunked_response(self, response):
        """Parse and decrypt an encrypted chunked response. Raise an error
        if the response is plaintext json"""
        try:
            # if the json() does not fail we have an error because
            # the expected response is a chunked json response
            return _raise_if_error(response.json())
        except ValueError:
            # json() failed so parse and decrypt the chunked response
            response = _parse_chunks(response.text)
            return _decrypt_chunks(response['payloads'],
                                   self.request_builder.crypto)


def _process_json_response(response):
    """Execute a post request and expect a JSON response"""
    try:
        return _raise_if_error(response.json())
    except ValueError:
        raise MSLError('Expected JSON response')


def _raise_if_error(decoded_response):
    if 'errordata' in decoded_response:
        raise MSLError(
            base64.standard_b64decode(decoded_response['errordata']))
    return decoded_response


def _parse_chunks(message):
    header = message.split('}}')[0] + '}}'
    payloads = re.split(',\"signature\":\"[0-9A-Za-z=/+]+\"}',
                        message.split('}}')[1])
    payloads = [x + '}' for x in payloads][:-1]
    return {'header': header, 'payloads': payloads}


def _decrypt_chunks(chunks, crypto):
    decrypted_payload = ''
    for chunk in chunks:
        payloadchunk = json.JSONDecoder().decode(chunk)
        payload = payloadchunk.get('payload')
        decoded_payload = base64.standard_b64decode(payload)
        encryption_envelope = json.JSONDecoder().decode(decoded_payload)
        # Decrypt the text
        plaintext = crypto.decrypt(
            base64.standard_b64decode(encryption_envelope['iv']),
            base64.standard_b64decode(encryption_envelope.get('ciphertext')))
        # unpad the plaintext
        plaintext = json.JSONDecoder().decode(plaintext)
        data = plaintext.get('data')

        # uncompress data if compressed
        if plaintext.get('compressionalgo') == 'GZIP':
            decoded_data = base64.standard_b64decode(data)
            data = zlib.decompress(decoded_data, 16 + zlib.MAX_WBITS)
        else:
            data = base64.standard_b64decode(data)
        decrypted_payload += data

    decrypted_payload = json.loads(decrypted_payload)[1]['payload']['data']
    decrypted_payload = base64.standard_b64decode(decrypted_payload)
    return json.JSONDecoder().decode(decrypted_payload)