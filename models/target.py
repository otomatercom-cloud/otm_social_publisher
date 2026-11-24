# -*- coding: utf-8 -*-
import logging

from odoo import fields, models, _

from . import meta_api

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class OtmSocialPostTarget(models.Model):
    _name = 'otm.social.post.target'
    _description = 'Social Post Result per Account'
    _order = 'id'

    post_id = fields.Many2one('otm.social.post', required=True, ondelete='cascade', index=True)
    account_id = fields.Many2one('otm.social.account', required=True, ondelete='restrict')
    platform = fields.Selection(related='account_id.platform', string='Platform', store=True)
    state = fields.Selection(
        [('pending', 'Pending'), ('processing', 'Processing'),
         ('posted', 'Posted'), ('failed', 'Failed')],
        default='pending', required=True, index=True)
    container_id = fields.Char(string='Instagram Container ID', copy=False)
    remote_id = fields.Char(string='Remote Post ID', copy=False)
    remote_url = fields.Char(string='Post Link', copy=False)
    error = fields.Text(copy=False)
    attempt_count = fields.Integer(copy=False)
    last_attempt = fields.Datetime(copy=False)

    _post_account_uniq = models.Constraint(
        'unique(post_id, account_id)', 'Each account can only be targeted once per post.')

    # ------------------------------------------------------------------
    def _run(self):
        """Publish (or advance) this target. Never raises for API problems."""
        self.ensure_one()
        self = self.sudo()
        if self.state not in ('pending', 'processing'):
            return
        # Row lock: the cron and a manual "Publish Now" must never both publish.
        self.flush_recordset()
        self.env.cr.execute(
            'SELECT id FROM otm_social_post_target WHERE id = %s FOR UPDATE SKIP LOCKED', [self.id])
        if not self.env.cr.fetchone():
            return
        account = self.account_id
        token = account.access_token
        try:
            if not account.active or not token:
                raise meta_api.MetaApiError(_('The account is archived or has no access token.'))
            if account.platform == 'facebook':
                self._run_facebook(account, token)
            else:
                self._run_instagram(account, token)
        except meta_api.MetaApiError as exc:
            self._register_failure(exc, account)
        except Exception as exc:  # noqa: BLE001 - never let one account block the others
            _logger.exception('Social Publisher: unexpected error on target %s', self.id)
            self._register_failure(
                meta_api.MetaApiError(str(exc) or exc.__class__.__name__, transient=True), account)

    def _run_facebook(self, account, token):
        post = self.post_id
        version, page_id, text = account.graph_version, account.external_id, post.caption or ''
        if post.media_type == 'text':
            remote_id = meta_api.fb_publish_text(version, token, page_id, text)
        elif post.media_type == 'image':
            remote_id = meta_api.fb_publish_photos(version, token, page_id, text, post._media_urls())
        else:
            remote_id = meta_api.fb_publish_video(version, token, page_id, text, post._media_urls()[0])
        if not remote_id:
            raise meta_api.MetaApiError(_('Facebook did not return a post id.'))
        self._mark_posted(remote_id, 'https://www.facebook.com/%s' % remote_id)

    def _run_instagram(self, account, token):
        post = self.post_id
        version, ig_id = account.graph_version, account.external_id
        if not self.container_id:
            caption = post.caption or ''
            urls = post._media_urls()
            if post.media_type == 'video':
                container = meta_api.ig_create_container(version, token, ig_id, {
                    'media_type': 'REELS', 'video_url': urls[0],
                    'caption': caption, 'share_to_feed': 'true'})
            elif len(urls) == 1:
                container = meta_api.ig_create_container(version, token, ig_id, {
                    'image_url': urls[0], 'caption': caption})
            else:
                children = []
                for url in urls:
                    child = meta_api.ig_create_container(version, token, ig_id, {
                        'image_url': url, 'is_carousel_item': 'true'})
                    if not meta_api.ig_wait_finished(version, token, child, max_wait=30):
                        raise meta_api.MetaApiError(
                            _('Instagram is still processing the carousel images.'), transient=True)
                    children.append(child)
                container = meta_api.ig_create_container(version, token, ig_id, {
                    'media_type': 'CAROUSEL', 'children': ','.join(children), 'caption': caption})
            if not container:
                raise meta_api.MetaApiError(_('Instagram did not return a container id.'))
            # Saved immediately so a retry never creates (and posts) a second container.
            self.write({'container_id': container, 'state': 'processing'})
        max_wait = 10 if post.media_type == 'video' else 20
        if not meta_api.ig_wait_finished(version, token, self.container_id, max_wait=max_wait):
            return  # still processing (large video) - the cron polls it again
        media_id, permalink = meta_api.ig_publish_container(version, token, ig_id, self.container_id)
        if not media_id:
            raise meta_api.MetaApiError(_('Instagram did not return a media id.'))
        self._mark_posted(media_id, permalink or 'https://www.instagram.com/')

    # ------------------------------------------------------------------
    def _mark_posted(self, remote_id, remote_url):
        self.write({
            'state': 'posted', 'remote_id': remote_id, 'remote_url': remote_url,
            'error': False, 'last_attempt': fields.Datetime.now(),
        })

    def _register_failure(self, exc, account):
        attempts = self.attempt_count + 1
        final = (not exc.transient) or attempts >= MAX_ATTEMPTS
        vals = {'attempt_count': attempts, 'last_attempt': fields.Datetime.now(), 'error': str(exc)[:1000]}
        if final:
            vals['state'] = 'failed'
        self.write(vals)
        if exc.is_auth_error:
            account.write({'connection_state': 'error', 'last_error': str(exc)[:250]})
