from unittest.mock import patch
from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestForumModeration(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.forum = cls.env['forum.forum'].search([], limit=1)

        if not cls.forum:
            cls.forum = cls.env['forum.forum'].create({
                'name': 'Test Forum'
            })

    def _make_post(self, name, content):
        return self.env['forum.post'].create({
            'name': name,
            'content': content,
            'forum_id': self.forum.id,
        })

    # ------------------------------------------------------------------
    # TEXT TESTS
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_toxic_text_blocked(self, mock_predict):
        """Score >= block threshold -> state should be 'offensive'."""

        mock_predict.return_value = {
            'toxicity': 0.98,
            'severe_toxicity': 0.23,
            'obscene': 0.42,
            'threat': 0.83,
            'insult': 0.59,
            'identity_attack': 0.04,
        }

        post = self._make_post(
            'I will destroy you',
            'You are a worthless idiot and I will hurt you badly.',
        )

        self.assertEqual(post.state, 'offensive')

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_clean_text_published(self, mock_predict):
        """Score below review threshold -> should NOT be blocked or flagged."""

        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0001,
            'obscene': 0.0002,
            'threat': 0.0001,
            'insult': 0.0002,
            'identity_attack': 0.0001,
        }

        post = self._make_post(
            'How do I improve my resume?',
            'Any tips on formatting or what skills to highlight?',
        )

        self.assertNotIn(
            post.state,
            ('offensive', 'flagged')
        )

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_borderline_text_flagged(self, mock_predict):
        """Score between review and block threshold -> state should be 'flagged'."""

        mock_predict.return_value = {
            'toxicity': 0.77,
            'severe_toxicity': 0.05,
            'obscene': 0.02,
            'threat': 0.10,
            'insult': 0.15,
            'identity_attack': 0.01,
        }

        post = self._make_post(
            'Above block threshold text',
            'I will destroy you',
        )

        self.assertEqual(post.state, 'flagged')

    # ------------------------------------------------------------------
    # IMAGE TESTS
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_explicit_image_blocked(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        NSFW service returns high Porn score
        -> state should be 'offensive'.
        """

        # Make text moderation safe so the image is tested independently.
        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        # IMPORTANT:
        # This response format matches forum_post.py:
        # result['predictions']
        # prediction['className']
        # prediction['probability']
        mock_requests_post.return_value.json.return_value = {
            'predictions': [
                {
                    'className': 'Porn',
                    'probability': 0.91
                },
                {
                    'className': 'Hentai',
                    'probability': 0.02
                },
                {
                    'className': 'Sexy',
                    'probability': 0.05
                },
                {
                    'className': 'Neutral',
                    'probability': 0.01
                },
                {
                    'className': 'Drawing',
                    'probability': 0.01
                },
            ]
        }

        content = (
            '<p>Check this out</p>'
            '<img src="data:image/png;base64,aGVsbG8gd29ybGQ=">'
        )

        post = self._make_post(
            'Image test post',
            content
        )

        self.assertEqual(
            post.state,
            'offensive'
        )

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_safe_image_published(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        NSFW service returns a Neutral score
        -> should NOT be blocked or flagged.
        """

        # Make text moderation safe.
        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        # Same response format expected by forum_post.py.
        mock_requests_post.return_value.json.return_value = {
            'predictions': [
                {
                    'className': 'Neutral',
                    'probability': 0.95
                },
                {
                    'className': 'Porn',
                    'probability': 0.01
                },
                {
                    'className': 'Hentai',
                    'probability': 0.01
                },
                {
                    'className': 'Sexy',
                    'probability': 0.02
                },
                {
                    'className': 'Drawing',
                    'probability': 0.01
                },
            ]
        }

        content = (
            '<p>Nice view today</p>'
            '<img src="data:image/png;base64,aGVsbG8gd29ybGQ=">'
        )

        post = self._make_post(
            'Safe image test post',
            content
        )

        self.assertNotIn(
            post.state,
            ('offensive', 'flagged')
        )

    # ------------------------------------------------------------------
    # ROBUSTNESS / FAIL-OPEN TESTS
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_text_model_failure_does_not_crash_post_creation(
        self,
        mock_predict
    ):
        """
        If Detoxify throws an exception,
        post creation should still succeed.
        """

        mock_predict.side_effect = Exception(
            "model unavailable"
        )

        post = self._make_post(
            'Service down test',
            'Some normal text here.'
        )

        self.assertTrue(post.exists())

        self.assertNotIn(
            post.state,
            ('offensive', 'flagged')
        )

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.'
        'ForumPost._text_model_predict'
    )
    def test_image_service_down_does_not_crash_post_creation(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        If the NSFW microservice is unreachable,
        post creation should still succeed.
        """

        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        mock_requests_post.side_effect = Exception(
            "Connection refused"
        )

        content = (
            '<p>Testing a down service</p>'
            '<img src="data:image/png;base64,aGVsbG8gd29ybGQ=">'
        )

        post = self._make_post(
            'Image service unreachable test',
            content
        )

        self.assertTrue(post.exists())

        self.assertNotIn(
            post.state,
            ('offensive', 'flagged')
        )