import base64
from odoo import http, _
from odoo.http import request
from odoo.tools import is_html_empty
from odoo.addons.website_forum.controllers.website_forum import WebsiteForum


class WebsiteForumModerationNotice(WebsiteForum):

    @http.route(['/forum/<model("forum.forum"):forum>/new',
                 '/forum/<model("forum.forum"):forum>/<model("forum.post"):post_parent>/reply'],
                type='http', auth="user", methods=['POST'], website=True)
    def post_create(self, forum, post_parent=None, **post):
        if is_html_empty(post.get('content', '')):
            return request.render('http_routing.http_error', {
                'status_code': _('Bad Request'),
                'status_message': post_parent and _('Reply should not be empty.') or _('Question should not be empty.')
            })

        post_tag_ids = forum._tag_to_write_vals(post.get('post_tags', ''))
        slug = request.env['ir.http']._slug

        if forum.has_pending_post:
            return request.redirect("/forum/%s/ask" % slug(forum))

        new_question = request.env['forum.post'].create({
            'forum_id': forum.id,
            'name': post.get('post_name') or (post_parent and 'Re: %s' % (post_parent.name or '')) or '',
            'content': post.get('content', False),
            'parent_id': post_parent and post_parent.id or False,
            'tag_ids': post_tag_ids
        })
        if post_parent:
            post_parent._update_last_activity()

        if new_question.state == 'pending':
            return request.redirect(f'/forum/{slug(forum)}/ask?moderation_notice=pending')
        elif new_question.state == 'offensive':
            return request.redirect(f'/forum/{slug(forum)}/ask?moderation_notice=blocked')
        elif new_question.state == 'flagged':
            return request.redirect(f'/forum/{slug(forum)}/ask?moderation_notice=flagged')

        target = slug(post_parent) if post_parent else new_question.id
        return request.redirect(f'/forum/{slug(forum)}/{target}')


class ModerationCheckController(http.Controller):

    @http.route('/api/moderation/check-text', type='jsonrpc', auth='user', methods=['POST'], csrf=False)
    def check_text(self, **kwargs):
        text = kwargs.get('text', '')
        if not text:
            return {'status': 'error', 'error': 'Missing "text" field'}

        moderation = request.env['content.moderation.mixin']

        try:
            level, reason = moderation.check_text(text, raise_on_error=True)
        except Exception as e:
            return {'status': 'error', 'error': f'Moderation check failed: {str(e)}'}

        return {
            'status': 'ok',
            'level': level or 'clean',
            'reason': reason or 'No issues detected',
        }

    @http.route('/api/moderation/check-image', type='jsonrpc', auth='user', methods=['POST'], csrf=False)
    def check_image(self, **kwargs):
        image_b64 = kwargs.get('image_base64', '')
        filename = kwargs.get('filename', 'upload.png')
        mimetype = kwargs.get('mimetype', 'image/png')

        if not image_b64:
            return {'status': 'error', 'error': 'Missing "image_base64" field'}

        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception:
            return {'status': 'error', 'error': 'Invalid base64 image data'}

        moderation = request.env['content.moderation.mixin']

        try:
            level, reason = moderation.check_image_bytes(
                image_bytes, filename, mimetype, raise_on_error=True
            )
        except Exception as e:
            return {'status': 'error', 'error': f'Moderation check failed: {str(e)}'}

        return {
            'status': 'ok',
            'level': level or 'clean',
            'reason': reason or 'No issues detected',
        }