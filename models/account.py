# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from . import meta_api


class OtmSocialAccount(models.Model):
    _name = 'otm.social.account'
    _description = 'Social Publishing Account'
    _order = 'platform, name'

    name = fields.Char(required=True)
    platform = fields.Selection(
        [('facebook', 'Facebook Page'), ('instagram', 'Instagram Business')],
        required=True, default='facebook')
    external_id = fields.Char(
        string='Page / Account ID', required=True,
        help='Facebook: the Page ID. Instagram: the Instagram Business Account ID '
             '(linked to a Facebook Page).')
    access_token = fields.Char(
        string='Page Access Token', groups='otm_social_publisher.group_manager',
        help='Long-lived Page access token with pages_manage_posts, '
             'pages_read_engagement and instagram_content_publish permissions.')
    has_token = fields.Boolean(string='Token Saved', compute='_compute_has_token')
    graph_version = fields.Char(string='Graph API Version', default='v23.0', required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    connection_state = fields.Selection(
        [('unknown', 'Not Tested'), ('ok', 'Connected'), ('error', 'Error')],
        default='unknown', readonly=True)
    last_check = fields.Datetime(string='Last Connection Test', readonly=True)
    last_error = fields.Char(readonly=True)

    _account_uniq = models.Constraint(
        'unique(platform, external_id, company_id)',
        'This account is already configured for this company.')

    def _compute_has_token(self):
        for rec in self:
            rec.has_token = bool(rec.sudo().access_token)

    @api.depends('name', 'platform')
    def _compute_display_name(self):
        labels = dict(self._fields['platform'].selection)
        for rec in self:
            rec.display_name = '%s (%s)' % (rec.name or '', labels.get(rec.platform, ''))

    def action_test_connection(self):
        self.ensure_one()
        token = self.sudo().access_token
        if not token:
            raise UserError(_('Please save the access token first.'))
        now = fields.Datetime.now()
        try:
            label = meta_api.check_connection(self.graph_version, token, self.external_id, self.platform)
        except meta_api.MetaApiError as exc:
            self.write({'connection_state': 'error', 'last_check': now, 'last_error': str(exc)[:250]})
            return self._notify(_('Connection failed'), str(exc), 'danger')
        self.write({'connection_state': 'ok', 'last_check': now, 'last_error': False})
        return self._notify(_('Connected'), _('Reached "%s" successfully.', label), 'success')

    @staticmethod
    def _notify(title, message, kind):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': kind, 'sticky': kind == 'danger'},
        }
