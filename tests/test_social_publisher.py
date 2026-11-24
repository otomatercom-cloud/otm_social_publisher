# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

JPG = b'\xff\xd8\xff\xe0' + b'0' * 64
MP4 = b'\x00\x00\x00\x18ftypmp42' + b'0' * 64
FILES = {
    'jpg': ('poster.jpg', 'image/jpeg', JPG),
    'jpg2': ('poster2.jpg', 'image/jpeg', JPG),
    'png': ('poster.png', 'image/png', JPG),
    'mp4': ('reel.mp4', 'video/mp4', MP4),
}
API = 'odoo.addons.otm_social_publisher.models.meta_api'


class FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


class FakeGraph:
    """Records calls and answers like the Graph API."""

    def __init__(self):
        self.calls = []
        self.container_status = 'FINISHED'
        self.error = None  # (code, message) -> every call fails

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            code, msg = self.error
            return FakeResp({'error': {'message': msg, 'code': code}}, 400)
        path = url.split('/v23.0/')[1]
        if path.endswith('/photos'):
            return FakeResp({'id': 'ph1', 'post_id': '100_200'})
        if path.endswith('/feed'):
            return FakeResp({'id': '100_300'})
        if path.endswith('/videos'):
            return FakeResp({'id': 'vid1'})
        if path.endswith('/media'):
            return FakeResp({'id': 'cont1'})
        if path.endswith('/media_publish'):
            return FakeResp({'id': 'igm1'})
        if path == 'cont1':
            return FakeResp({'status_code': self.container_status})
        if path == 'igm1':
            return FakeResp({'permalink': 'https://www.instagram.com/p/abc/'})
        return FakeResp({'name': 'My Page', 'username': 'mypage'})

    def paths(self):
        return [c[1].split('/v23.0/')[1] for c in self.calls]


@tagged('post_install', '-at_install')
class TestSocialPublisher(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://erp.example.com')
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def mk(login, group):
            return Users.create({
                'name': login, 'login': login, 'email': '%s@example.com' % login,
                'group_ids': [(6, 0, [cls.env.ref('otm_social_publisher.' + group).id])],
            })
        cls.requester = mk('sp_requester', 'group_user')
        cls.approver = mk('sp_approver', 'group_approver')
        cls.manager = mk('sp_manager', 'group_manager')
        cls.fb = cls.env['otm.social.account'].create({
            'name': 'Brand Page', 'platform': 'facebook', 'external_id': '111', 'access_token': 'TOKEN_FB'})
        cls.ig = cls.env['otm.social.account'].create({
            'name': 'Brand IG', 'platform': 'instagram', 'external_id': '222', 'access_token': 'TOKEN_IG'})

    def _post(self, accounts, media_type='image', files=('jpg',), user=None, **kw):
        Post = self.env['otm.social.post'].with_user(user or self.requester)
        vals = {
            'title': 'Test', 'caption': 'Hello #odoo', 'media_type': media_type,
            'account_ids': [(6, 0, accounts.ids)],
            # attachments are created by the requester, like a real upload
            'attachment_ids': [(0, 0, dict(zip(('name', 'mimetype', 'raw'), FILES[key]))) for key in files],
        }
        vals.update(kw)
        return Post.create(vals)

    def _approve(self, post):
        post.with_user(self.requester).action_submit()
        post.with_user(self.approver).action_approve()

    def _publish(self, post, graph):
        with patch(API + '.requests.request', side_effect=graph), patch(API + '.time.sleep'):
            post.with_user(self.approver).action_publish_now()

    # ------------------------------------------------------------------
    def test_facebook_image_flow(self):
        post = self._post(self.fb)
        self.assertEqual(post.state, 'draft')
        self.assertEqual(post.with_user(self.approver).attachment_ids.mapped('name'), ['poster.jpg'])
        self._approve(post)
        self.assertEqual(post.state, 'approved')
        self.assertEqual(post.approved_by, self.approver)
        self.assertEqual(len(post.target_ids), 1)
        graph = FakeGraph()
        self._publish(post, graph)
        self.assertEqual(post.state, 'posted')
        self.assertEqual(post.target_ids.remote_id, '100_200')
        method, url, kwargs = graph.calls[0]
        self.assertEqual(method, 'POST')
        self.assertTrue(kwargs['data']['url'].startswith('https://erp.example.com/web/content/'))
        self.assertIn('access_token=', kwargs['data']['url'])

    def test_facebook_multi_photo_and_video_and_text(self):
        post = self._post(self.fb, files=('jpg', 'jpg2'))
        self._approve(post)
        graph = FakeGraph()
        self._publish(post, graph)
        self.assertEqual(graph.paths(), ['111/photos', '111/photos', '111/feed'])
        self.assertEqual(post.state, 'posted')
        video = self._post(self.fb, 'video', ('mp4',))
        self._approve(video)
        graph = FakeGraph()
        self._publish(video, graph)
        self.assertEqual(graph.paths(), ['111/videos'])
        text = self._post(self.fb, 'text', ())
        self._approve(text)
        graph = FakeGraph()
        self._publish(text, graph)
        self.assertEqual(graph.paths(), ['111/feed'])

    def test_instagram_image_and_carousel(self):
        post = self._post(self.ig)
        self._approve(post)
        graph = FakeGraph()
        self._publish(post, graph)
        self.assertEqual(post.state, 'posted')
        self.assertEqual(post.target_ids.remote_url, 'https://www.instagram.com/p/abc/')
        self.assertIn('222/media_publish', graph.paths())
        carousel = self._post(self.ig, files=('jpg', 'jpg2'))
        self._approve(carousel)
        graph = FakeGraph()
        self._publish(carousel, graph)
        self.assertEqual(graph.paths().count('222/media'), 3)  # 2 children + parent
        self.assertEqual(carousel.state, 'posted')

    def test_instagram_video_processes_async(self):
        post = self._post(self.ig, 'video', ('mp4',))
        self._approve(post)
        graph = FakeGraph()
        graph.container_status = 'IN_PROGRESS'
        self._publish(post, graph)
        self.assertEqual(post.state, 'publishing')
        self.assertEqual(post.target_ids.state, 'processing')
        self.assertEqual(post.target_ids.container_id, 'cont1')
        graph = FakeGraph()  # container finished now; cron picks it up, no second container
        with patch(API + '.requests.request', side_effect=graph), patch(API + '.time.sleep'):
            self.env['otm.social.post']._cron_publish_due()
        self.assertEqual(post.state, 'posted')
        self.assertNotIn('222/media', graph.paths())

    def test_both_platforms_partial_then_retry(self):
        post = self._post(self.fb + self.ig)
        self._approve(post)

        class Flaky(FakeGraph):
            def __call__(self, method, url, **kwargs):
                if '/222/' in url:
                    self.calls.append((method, url, kwargs))
                    return FakeResp({'error': {'message': 'Bad media', 'code': 100}}, 400)
                return super().__call__(method, url, **kwargs)
        self._publish(post, Flaky())
        self.assertEqual(post.state, 'partial')
        failed = post.target_ids.filtered(lambda t: t.state == 'failed')
        self.assertEqual(failed.account_id, self.ig)
        self.assertIn('Bad media', failed.error)
        post.with_user(self.approver).action_retry_failed()
        self.assertEqual(post.state, 'approved')
        graph = FakeGraph()
        self._publish(post, graph)
        self.assertEqual(post.state, 'posted')
        self.assertNotIn('111/photos', graph.paths())  # facebook is not posted twice

    def test_transient_error_gives_up_after_three_attempts(self):
        post = self._post(self.fb)
        self._approve(post)
        graph = FakeGraph()
        graph.error = (4, 'Rate limit reached')
        for _i in range(2):
            self._publish(post, graph)
            self.assertEqual(post.target_ids.state, 'pending')
        self._publish(post, graph)
        self.assertEqual(post.target_ids.state, 'failed')
        self.assertEqual(post.state, 'failed')

    def test_auth_error_flags_account_and_token_is_scrubbed(self):
        post = self._post(self.fb)
        self._approve(post)
        graph = FakeGraph()
        graph.error = (190, 'Invalid OAuth access token TOKEN_FB')
        self._publish(post, graph)
        self.assertEqual(post.target_ids.state, 'failed')
        self.assertEqual(self.fb.connection_state, 'error')
        self.assertNotIn('TOKEN_FB', post.target_ids.error)

    # ------------------------------------------------------------------
    def test_approval_rules(self):
        post = self._post(self.fb)
        post.with_user(self.requester).action_submit()
        with self.assertRaises(AccessError):
            post.with_user(self.requester).action_approve()
        own = self._post(self.fb, user=self.approver)
        own.with_user(self.approver).action_submit()
        with self.assertRaises(UserError):
            own.with_user(self.approver).action_approve()
        own.with_user(self.manager).action_approve()
        self.assertEqual(own.state, 'approved')

    def test_reject_and_content_lock(self):
        post = self._post(self.fb)
        post.with_user(self.requester).action_submit()
        with self.assertRaises(UserError):
            post.with_user(self.requester).write({'caption': 'sneaky edit'})
        with self.assertRaises(UserError):
            post.with_user(self.requester).write({'state': 'approved'})
        self.env['otm.social.post.reject.wizard'].with_user(self.approver).create({
            'post_id': post.id, 'reason': 'Wrong date'}).action_confirm()
        self.assertEqual(post.state, 'rejected')
        self.assertEqual(post.reject_reason, 'Wrong date')
        post.with_user(self.requester).write({'caption': 'fixed'})
        post.with_user(self.requester).action_submit()
        self.assertEqual(post.state, 'to_approve')

    def test_validation(self):
        with self.assertRaises(UserError):  # Instagram needs JPG
            self._post(self.ig, files=('png',)).action_submit()
        with self.assertRaises(UserError):  # Instagram cannot do text only
            self._post(self.fb + self.ig, 'text', ()).action_submit()
        with self.assertRaises(UserError):  # no accounts
            self._post(self.env['otm.social.account']).action_submit()
        with self.assertRaises(UserError):  # video must be a single mp4
            self._post(self.fb, 'video', ('jpg',)).action_submit()
        with self.assertRaises(UserError):  # caption too long for Instagram
            self._post(self.ig, caption='x' * 2300).action_submit()

    def test_localhost_base_url_blocks_publishing(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://localhost:8069')
        post = self._post(self.fb)
        self._approve(post)
        self._publish(post, FakeGraph())
        self.assertEqual(post.state, 'failed')
        self.assertIn('web.base.url', post.target_ids.error)

    def test_token_hidden_from_normal_users(self):
        with self.assertRaises(AccessError):
            self.fb.with_user(self.requester).access_token
        self.assertTrue(self.fb.with_user(self.requester).has_token)
        self.assertEqual(self.fb.with_user(self.manager).access_token, 'TOKEN_FB')

    def test_requester_sees_only_own_posts(self):
        mine = self._post(self.fb)
        other = self._post(self.fb, user=self.approver)
        seen = self.env['otm.social.post'].with_user(self.requester).search([('id', 'in', (mine + other).ids)])
        self.assertEqual(seen, mine)
        seen = self.env['otm.social.post'].with_user(self.approver).search([('id', 'in', (mine + other).ids)])
        self.assertEqual(seen, mine + other)

    def test_connection_test_button(self):
        graph = FakeGraph()
        with patch(API + '.requests.request', side_effect=graph):
            res = self.fb.with_user(self.manager).action_test_connection()
        self.assertEqual(res['params']['type'], 'success')
        self.assertEqual(self.fb.connection_state, 'ok')
