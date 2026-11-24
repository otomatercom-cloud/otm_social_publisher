# -*- coding: utf-8 -*-
from odoo import fields, models


class OtmSocialPostRejectWizard(models.TransientModel):
    _name = 'otm.social.post.reject.wizard'
    _description = 'Reject Social Post'

    post_id = fields.Many2one('otm.social.post', required=True, readonly=True)
    reason = fields.Text(string='Reason', required=True)

    def action_confirm(self):
        self.ensure_one()
        self.post_id._do_reject(self.reason)
        return {'type': 'ir.actions.act_window_close'}
