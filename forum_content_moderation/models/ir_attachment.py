from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        attachments = super().create(vals_list)

        for attachment in attachments:
            if attachment.res_model != 'forum.post' or not attachment.res_id:
                continue
            if not attachment.mimetype or not attachment.mimetype.startswith('image/'):
                continue

            post = self.env['forum.post'].browse(attachment.res_id)
            if not post.exists():
                continue

            try:
                image_bytes = attachment.raw
                level, reason = post._check_image_nsfw_bytes(
                    image_bytes, attachment.name or 'attachment.png', attachment.mimetype
                )
                if level in ('block', 'review'):
                    post._apply_moderation_result(post, level, reason)
            except Exception as e:
                _logger.error(
                    "Attachment moderation check failed for attachment %s: %s",
                    attachment.id, e
                )

        return attachments