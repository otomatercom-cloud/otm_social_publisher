# -*- coding: utf-8 -*-
import logging
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.modules import module as odoo_module
from odoo.exceptions import AccessError, UserError

from . import meta_api

_logger = logging.getLogger(__name__)

GROUP_APPROVER = 'otm_social_publisher.group_approver'
GROUP_MANAGER = 'otm_social_publisher.group_manager'
CRON_XMLID = 'otm_social_publisher.ir_cron_publish_due'

# Fields that may only change while the post is still a draft (or was rejected).
CONTENT_FIELDS = {'caption', 'media_type', 'attachment_ids', 'account_ids'}
IG_CAPTION_MAX = 2200
IG_IMAGE_MAX_BYTES = 8 * 1024 * 1024
LOCAL_HOST_PREFIXES = ('localhost', '127.', '0.0.0.0', '192.168.', '10.')


class OtmSocialPost(models.Model):
    _name = 'otm.social.post'
    _description = 'Social Post'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'scheduled_datetime desc, id desc'

    name = fields.Char(string='Reference', default='New', copy=False, readonly=True)
    title = fields.Char(string='Title', required=True, tracking=True,
                        help='Internal title, never published.')
    caption = fields.Text(string='Caption')
    media_type = fields.Selection(
        [('image', 'Poster / Images'), ('video', 'Video / Reel'), ('text', 'Text Only')],
        required=True, default='image', tracking=True)
    attachment_ids = fields.Many2many(
        'ir.attachment', 'otm_social_post_attachment_rel', 'post_id', 'attachment_id',
        string='Media Files')
    account_ids = fields.Many2many(
        'otm.social.account', 'otm_social_post_account_rel', 'post_id', 'account_id',
        string='Publish To', domain="[('company_id', '=', company_id)]")
    scheduled_datetime = fields.Datetime(string='Scheduled For', tracking=True,
                                         help='Leave empty to publish right after approval.')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('to_approve', 'Waiting Approval'),
        ('approved', 'Approved'),
        ('publishing', 'Publishing'),
        ('posted', 'Posted'),
        ('partial', 'Partially Posted'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    requested_by = fields.Many2one('res.users', default=lambda self: self.env.user,
                                   readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    reject_reason = fields.Text(readonly=True, copy=False)
    published_date = fields.Datetime(readonly=True, copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    target_ids = fields.One2many('otm.social.post.target', 'post_id', string='Publishing Results')
    target_count = fields.Integer(compute='_compute_target_count')

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.depends('target_ids')
    def _compute_target_count(self):
        for post in self:
            post.target_count = len(post.target_ids)

    @api.depends('name', 'title')
    def _compute_display_name(self):
        for post in self:
            post.display_name = '%s - %s' % (post.name, post.title) if post.title else (post.name or '')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('otm.social.post') or 'New'
        posts = super().create(vals_list)
        posts._bind_attachments()
        return posts

    def write(self, vals):
        if not self.env.su:
            if 'state' in vals:
                raise UserError(_('The state can only be changed with the workflow buttons.'))
            if CONTENT_FIELDS & set(vals) and any(p.state not in ('draft', 'rejected') for p in self):
                raise UserError(_('Content can only be edited while the post is Draft or Rejected. '
                                  'Reset it to draft first.'))
        res = super().write(vals)
        if 'attachment_ids' in vals:
            self._bind_attachments()
        return res

    def _bind_attachments(self):
        """Attach uploads to the post so approvers (who did not upload them) can open them."""
        for post in self.sudo():
            loose = post.attachment_ids.filtered(
                lambda a: a.res_model != self._name or a.res_id != post.id)
            if loose:
                loose.write({'res_model': self._name, 'res_id': post.id})

    def unlink(self):
        if not self.env.su and any(p.state not in ('draft', 'rejected', 'cancelled') for p in self):
            raise UserError(_('Only draft, rejected or cancelled posts can be deleted.'))
        return super().unlink()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _check_ready(self):
        for post in self:
            accounts = post.account_ids
            if not accounts:
                raise UserError(_('%s: select at least one account to publish to.', post.name))
            has_ig = any(a.platform == 'instagram' for a in accounts)
            files = post.attachment_ids
            if post.media_type == 'text':
                if not (post.caption or '').strip():
                    raise UserError(_('%s: a text post needs a caption.', post.name))
                if has_ig:
                    raise UserError(_('%s: Instagram cannot publish text-only posts. '
                                      'Remove the Instagram account or add media.', post.name))
            elif post.media_type == 'image':
                if not 1 <= len(files) <= 10:
                    raise UserError(_('%s: attach between 1 and 10 images.', post.name))
                for att in files:
                    if att.mimetype not in ('image/jpeg', 'image/png'):
                        raise UserError(_('%s: "%s" must be a JPG or PNG image.', post.name, att.name))
                    if has_ig and att.mimetype != 'image/jpeg':
                        raise UserError(_('%s: Instagram accepts JPG images only. Convert "%s" to JPG.',
                                          post.name, att.name))
                    if has_ig and att.file_size > IG_IMAGE_MAX_BYTES:
                        raise UserError(_('%s: "%s" is larger than 8 MB (Instagram limit).',
                                          post.name, att.name))
            else:
                if len(files) != 1:
                    raise UserError(_('%s: attach exactly one video file.', post.name))
                if files.mimetype not in ('video/mp4', 'video/quicktime'):
                    raise UserError(_('%s: the video must be MP4 or MOV.', post.name))
            if has_ig and len(post.caption or '') > IG_CAPTION_MAX:
                raise UserError(_('%s: Instagram captions are limited to %s characters.',
                                  post.name, IG_CAPTION_MAX))

    def _media_urls(self):
        """Public, token-protected URLs Meta downloads the media from."""
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        host = base.split('://', 1)[-1]
        if not base.startswith('http') or host.startswith(LOCAL_HOST_PREFIXES):
            raise meta_api.MetaApiError(
                _('System parameter web.base.url must be the public URL of this server '
                  '(Meta cannot download media from "%s").', base or '-'))
        urls = []
        for att in self.attachment_ids.sorted('id').sudo():
            att.generate_access_token()
            urls.append('%s/web/content/%s/%s?access_token=%s&download=true' % (
                base, att.id, quote(att.name or 'media'), att.access_token))
        if not urls:
            raise meta_api.MetaApiError(_('No media files attached.'))
        return urls

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _check_approver(self):
        if not self.env.user.has_group(GROUP_APPROVER):
            raise AccessError(_('Only approvers can do this.'))

    def _trigger_cron(self):
        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def action_submit(self):
        for post in self:
            if post.state not in ('draft', 'rejected'):
                raise UserError(_('%s cannot be submitted from state "%s".', post.name, post.state))
        self._check_ready()
        self.sudo().write({'state': 'to_approve', 'reject_reason': False})
        approvers = self.env.ref(GROUP_APPROVER).sudo().all_user_ids.filtered(lambda u: u.active)
        for post in self.sudo():
            post.activity_unlink(['mail.mail_activity_data_todo'])
            for user in approvers:
                if user != post.requested_by:
                    post.activity_schedule(
                        'mail.mail_activity_data_todo', user_id=user.id,
                        summary=_('Approve social post %s', post.name))
            post.message_post(body=_('Submitted for approval.'))
        return True

    def action_approve(self):
        self._check_approver()
        is_manager = self.env.user.has_group(GROUP_MANAGER)
        for post in self:
            if post.state != 'to_approve':
                raise UserError(_('%s is not waiting for approval.', post.name))
            if post.requested_by == self.env.user and not is_manager:
                raise UserError(_('You cannot approve your own post.'))
        self._check_ready()
        self.sudo().write({
            'state': 'approved', 'approved_by': self.env.uid,
            'approved_date': fields.Datetime.now(), 'reject_reason': False,
        })
        self._ensure_targets()
        for post in self.sudo():
            post.activity_unlink(['mail.mail_activity_data_todo'])
            post.message_post(
                body=_('Approved by %s.', self.env.user.name),
                partner_ids=post.requested_by.partner_id.ids)
        if any(not p.scheduled_datetime for p in self):
            self._trigger_cron()
        return True

    def action_reject(self):
        self._check_approver()
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reject Post'),
            'res_model': 'otm.social.post.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_post_id': self.id},
        }

    def _do_reject(self, reason):
        self._check_approver()
        for post in self:
            if post.state != 'to_approve':
                raise UserError(_('%s is not waiting for approval.', post.name))
        self.sudo().write({'state': 'rejected', 'reject_reason': reason})
        for post in self.sudo():
            post.activity_unlink(['mail.mail_activity_data_todo'])
            post.message_post(
                body=_('Rejected by %s: %s', self.env.user.name, reason),
                partner_ids=post.requested_by.partner_id.ids)

    def action_reset_draft(self):
        for post in self:
            if post.state not in ('to_approve', 'rejected', 'cancelled'):
                raise UserError(_('%s cannot be reset to draft from state "%s".', post.name, post.state))
        self.sudo().write({'state': 'draft'})
        self.sudo().activity_unlink(['mail.mail_activity_data_todo'])
        return True

    def action_cancel(self):
        for post in self:
            if post.state not in ('draft', 'to_approve', 'approved', 'rejected'):
                raise UserError(_('%s cannot be cancelled from state "%s".', post.name, post.state))
            if post.state == 'approved':
                self._check_approver()
        self.sudo().write({'state': 'cancelled'})
        self.sudo().target_ids.filtered(lambda t: t.state == 'pending').unlink()
        self.sudo().activity_unlink(['mail.mail_activity_data_todo'])
        return True

    def action_publish_now(self):
        self._check_approver()
        for post in self:
            if post.state not in ('approved', 'publishing'):
                raise UserError(_('%s is not approved yet.', post.name))
        self._check_ready()
        self._publish_targets()
        return True

    def action_retry_failed(self):
        self._check_approver()
        for post in self:
            if post.state not in ('failed', 'partial'):
                raise UserError(_('%s has nothing to retry.', post.name))
        for post in self.sudo():
            post.target_ids.filtered(lambda t: t.state == 'failed').write({
                'state': 'pending', 'attempt_count': 0, 'error': False, 'container_id': False})
        self.sudo().write({'state': 'approved'})
        self._trigger_cron()
        return True

    # ------------------------------------------------------------------
    # Publishing engine
    # ------------------------------------------------------------------
    def _ensure_targets(self):
        Target = self.env['otm.social.post.target'].sudo()
        for post in self:
            for account in post.account_ids - post.target_ids.account_id:
                Target.create({'post_id': post.id, 'account_id': account.id})

    def _publish_targets(self, commit=False):
        for post in self:
            post._ensure_targets()
            if post.state == 'approved':
                post.sudo().write({'state': 'publishing'})
            for target in post.target_ids.filtered(lambda t: t.state in ('pending', 'processing')):
                target._run()
                if commit:
                    self._commit()
            post._update_state_from_targets()
            if commit:
                self._commit()

    def _commit(self):
        # Persist progress per target so a crash later never loses a post that already went live.
        if not odoo_module.current_test:
            self.env.cr.commit()

    def _update_state_from_targets(self):
        for post in self:
            states = set(post.target_ids.mapped('state'))
            if not states:
                continue
            if states & {'pending', 'processing'}:
                new_state = 'publishing'
            elif states == {'posted'}:
                new_state = 'posted'
            elif states == {'failed'}:
                new_state = 'failed'
            else:
                new_state = 'partial'
            vals = {'state': new_state}
            if new_state in ('posted', 'partial', 'failed'):
                vals['published_date'] = fields.Datetime.now()
            if new_state != post.state or 'published_date' in vals:
                post.sudo().write(vals)

    @api.model
    def _cron_publish_due(self):
        now = fields.Datetime.now()
        posts = self.search([
            ('state', 'in', ('approved', 'publishing')),
            '|', ('scheduled_datetime', '=', False), ('scheduled_datetime', '<=', now),
        ], limit=20, order='scheduled_datetime, id')
        for post in posts:
            try:
                post._publish_targets(commit=True)
            except Exception:
                _logger.exception('Social Publisher: failed to process post %s', post.id)
                if odoo_module.current_test:
                    raise
                self.env.cr.rollback()
                self.env.invalidate_all()
        return True
