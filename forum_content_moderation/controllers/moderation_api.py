import base64
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ForumModerationAPI(http.Controller):

    @http.route(
        "/api/moderate",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False
    )
    def moderate(self, **kwargs):

        _logger.info("========== /api/moderate CALLED ==========")

        try:
            results = []

            # =====================================================
            # CHECK REQUEST TYPE
            # =====================================================

            content_type = request.httprequest.content_type or ""

            text = None
            image = None
            filename = "image.png"
            mimetype = "image/png"

            # =====================================================
            # JSON REQUEST
            # =====================================================

            if "application/json" in content_type:

                raw_data = request.httprequest.data or b"{}"

                try:
                    data = json.loads(
                        raw_data.decode("utf-8")
                    )
                except json.JSONDecodeError:
                    return request.make_json_response(
                        {
                            "success": False,
                            "message": "Invalid JSON request"
                        },
                        status=400
                    )

                text = data.get("text")
                image = data.get("image")
                filename = data.get(
                    "filename",
                    "image.png"
                )
                mimetype = data.get(
                    "mimetype",
                    "image/png"
                )

            # =====================================================
            # MULTIPART/FORM-DATA REQUEST
            # =====================================================

            else:

                text = (
                    request.httprequest.form.get("text")
                )

                image_file = (
                    request.httprequest.files.get("image")
                )

                if image_file:

                    _logger.info(
                        "Received uploaded image: %s",
                        image_file.filename
                    )

                    image = image_file.read()

                    filename = (
                        image_file.filename
                        or "image.png"
                    )

                    mimetype = (
                        image_file.mimetype
                        or "image/png"
                    )

            # =====================================================
            # TEXT MODERATION
            # =====================================================

            if text:

                _logger.info(
                    "Moderating text"
                )

                post_model = (
                    request.env["forum.post"].sudo()
                )

                action, reason = post_model.check_text(
                    text,
                    raise_on_error=True
                )

                results.append(
                    {
                        "type": "text",
                        "action": action or "allow",
                        "reason": reason or ""
                    }
                )

            # =====================================================
            # IMAGE MODERATION
            # =====================================================

            if image:

                _logger.info(
                    "Moderating image"
                )

                try:

                    # ---------------------------------------------
                    # JSON BASE64 IMAGE
                    # ---------------------------------------------

                    if isinstance(image, str):

                        image_bytes = base64.b64decode(
                            image
                        )

                    # ---------------------------------------------
                    # MULTIPART UPLOADED IMAGE
                    # ---------------------------------------------

                    else:

                        image_bytes = image

                    post_model = (
                        request.env["forum.post"].sudo()
                    )

                    action, reason = (
                        post_model.check_image_bytes(
                            image_bytes,
                            filename=filename,
                            mimetype=mimetype,
                            raise_on_error=True
                        )
                    )

                    results.append(
                        {
                            "type": "image",
                            "action": action or "allow",
                            "reason": reason or ""
                        }
                    )

                except Exception as image_error:

                    _logger.exception(
                        "Image moderation failed"
                    )

                    results.append(
                        {
                            "type": "image",
                            "action": "review",
                            "reason": str(image_error)
                        }
                    )

            # =====================================================
            # NO INPUT
            # =====================================================

            if not results:

                return request.make_json_response(
                    {
                        "success": False,
                        "message": "Provide text or image"
                    },
                    status=400
                )

            # =====================================================
            # FINAL MODERATION DECISION
            # =====================================================

            actions = [
                result["action"]
                for result in results
            ]

            if "block" in actions:

                final_action = "block"

            elif "review" in actions:

                final_action = "review"

            else:

                final_action = "allow"

            # =====================================================
            # RESPONSE
            # =====================================================

            response = {
                "success": True,
                "action": final_action,
                "results": results
            }

            _logger.info(
                "Moderation result: %s",
                response
            )

            return request.make_json_response(
                response,
                status=200
            )

        # =========================================================
        # GENERAL ERROR
        # =========================================================

        except Exception as e:

            _logger.exception(
                "Forum moderation API failed"
            )

            return request.make_json_response(
                {
                    "success": False,
                    "action": "review",
                    "error": str(e)
                },
                status=500
            )