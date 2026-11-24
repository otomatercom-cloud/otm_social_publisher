# -*- coding: utf-8 -*-
"""Thin Meta Graph API client (Facebook Pages + Instagram Content Publishing).

Plain functions, no ORM. Every network call goes through ``_call`` so tokens
are scrubbed from any error text before it can reach a log or a record.
"""
import json
import logging
import time

import requests

_logger = logging.getLogger(__name__)

GRAPH_URL = 'https://graph.facebook.com'
# Meta error codes that are worth retrying (rate limit / temporary issues).
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 368, 613}
# Meta error codes meaning the token is invalid / expired / lacks permission.
AUTH_CODES = {102, 190}


class MetaApiError(Exception):
    def __init__(self, message, code=None, transient=False):
        super().__init__(message)
        self.code = code
        self.transient = transient

    @property
    def is_auth_error(self):
        return self.code in AUTH_CODES


def _scrub(text, token):
    text = text or ''
    return text.replace(token, '***') if token else text


def _call(method, version, path, token, params=None, timeout=60):
    url = '%s/%s/%s' % (GRAPH_URL, version, path.lstrip('/'))
    payload = dict(params or {})
    payload['access_token'] = token
    kwargs = {'timeout': timeout}
    if method == 'GET':
        kwargs['params'] = payload
    else:
        kwargs['data'] = payload
    try:
        resp = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise MetaApiError(_scrub(str(exc), token), transient=True) from None
    try:
        body = resp.json()
    except ValueError:
        raise MetaApiError(
            'Unexpected response from Meta (HTTP %s)' % resp.status_code,
            transient=resp.status_code >= 500) from None
    if resp.status_code >= 400 or (isinstance(body, dict) and 'error' in body):
        err = (body.get('error') if isinstance(body, dict) else None) or {}
        code = err.get('code')
        transient = bool(err.get('is_transient')) or code in TRANSIENT_CODES or resp.status_code >= 500
        message = err.get('error_user_msg') or err.get('message') or 'HTTP %s' % resp.status_code
        raise MetaApiError(_scrub(message, token), code=code, transient=transient)
    return body


# --------------------------------------------------------------------------
# Connection test
# --------------------------------------------------------------------------
def check_connection(version, token, external_id, platform):
    field = 'name' if platform == 'facebook' else 'username'
    body = _call('GET', version, external_id, token, {'fields': field})
    return body.get(field) or external_id


# --------------------------------------------------------------------------
# Facebook Page
# --------------------------------------------------------------------------
def fb_publish_text(version, token, page_id, message):
    body = _call('POST', version, '%s/feed' % page_id, token, {'message': message})
    return body.get('id')


def fb_publish_photos(version, token, page_id, message, urls):
    if len(urls) == 1:
        body = _call('POST', version, '%s/photos' % page_id, token,
                     {'url': urls[0], 'caption': message})
        return body.get('post_id') or body.get('id')
    attached = []
    for url in urls:
        body = _call('POST', version, '%s/photos' % page_id, token,
                     {'url': url, 'published': 'false'})
        attached.append({'media_fbid': body.get('id')})
    body = _call('POST', version, '%s/feed' % page_id, token,
                 {'message': message, 'attached_media': json.dumps(attached)})
    return body.get('id')


def fb_publish_video(version, token, page_id, message, url):
    body = _call('POST', version, '%s/videos' % page_id, token,
                 {'file_url': url, 'description': message}, timeout=120)
    return body.get('id')


# --------------------------------------------------------------------------
# Instagram (Content Publishing API: create container -> wait -> publish)
# --------------------------------------------------------------------------
def ig_create_container(version, token, ig_id, params):
    body = _call('POST', version, '%s/media' % ig_id, token, params)
    return body.get('id')


def ig_container_status(version, token, container_id):
    body = _call('GET', version, container_id, token, {'fields': 'status_code,status'})
    return body.get('status_code'), body.get('status')


def ig_wait_finished(version, token, container_id, max_wait=20, interval=2):
    """True when the container is ready, False if still processing after max_wait."""
    waited = 0
    while True:
        status, detail = ig_container_status(version, token, container_id)
        if status in ('FINISHED', 'PUBLISHED'):
            return True
        if status in ('ERROR', 'EXPIRED'):
            raise MetaApiError('Instagram could not process the media (%s %s)' % (status, detail or ''))
        if waited >= max_wait:
            return False
        time.sleep(interval)
        waited += interval


def ig_publish_container(version, token, ig_id, container_id):
    body = _call('POST', version, '%s/media_publish' % ig_id, token, {'creation_id': container_id})
    media_id = body.get('id')
    permalink = None
    try:
        info = _call('GET', version, media_id, token, {'fields': 'permalink'})
        permalink = info.get('permalink')
    except MetaApiError:
        _logger.info('Could not fetch permalink for Instagram media %s', media_id)
    return media_id, permalink
