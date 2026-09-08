from odoo import models, fields


class ForumModerationFeedback(models.Model):
    _name = 'forum.moderation.feedback'
    _description = 'Moderation Override Log'
    _order = 'create_date desc'

    post_id = fields.Many2one('forum.post', string='Forum Post', required=True, ondelete='cascade')
    original_state = fields.Char(string='Original (Automated) State', required=True)
    corrected_state = fields.Char(string='Corrected State', required=True)
    corrected_by = fields.Many2one('res.users', string='Corrected By')
    create_date = fields.Datetime(string='Logged On', readonly=True)