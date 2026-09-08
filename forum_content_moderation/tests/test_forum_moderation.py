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
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_toxic_text_held_in_pending(self, mock_predict):
        """Score >= block threshold -> state should be held in 'pending' awaiting admin approval."""

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

        self.assertEqual(post.state, 'pending')
        self.assertTrue(post.moderation_reason)

        # Admin approves post -> state becomes 'active'
        post.action_approve_moderation()
        self.assertEqual(post.state, 'active')
        self.assertTrue(post.active)

    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_toxic_text_rejected_becomes_offensive(self, mock_predict):
        """When admin rejects a flagged/pending post, state should transition to 'offensive'."""

        mock_predict.return_value = {
            'toxicity': 0.95,
            'severe_toxicity': 0.20,
            'obscene': 0.30,
            'threat': 0.70,
            'insult': 0.50,
            'identity_attack': 0.02,
        }

        post = self._make_post(
            'Hostile Title',
            'Toxic hostile post content.',
        )

        self.assertEqual(post.state, 'pending')

        # Admin rejects post -> state becomes 'offensive'
        post.action_reject_moderation()
        self.assertEqual(post.state, 'offensive')
        self.assertFalse(post.active)

    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_clean_text_published(self, mock_predict):
        """Score below review threshold -> should be published immediately ('active')."""

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
            ('pending', 'offensive', 'flagged')
        )
        self.assertEqual(post.state, 'active')

    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_safe_short_posts_remain_active(self, mock_predict):
        """Safe short posts must NOT be flagged as illegal; they should be published directly."""

        mock_predict.return_value = {
            'toxicity': 0.0008,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        short_post_samples = [
            ('Need help with my computer', 'Can anyone help me diagnose why my laptop is running slow?'),
            ('Selling my old bicycle', 'Selling my old bicycle, pick up in town.'),
            ('Thanks for the advice', 'Thanks for the advice, have a great day!'),
            ('Where can I buy a phone?', 'Looking for phone recommendations.'),
        ]

        for title, content in short_post_samples:
            post = self._make_post(title, content)
            self.assertEqual(
                post.state, 'active',
                f"Safe post '{title}' should remain active but was {post.state}"
            )

    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
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
            'Borderline text',
            'Slightly aggressive text.',
        )

        self.assertEqual(post.state, 'flagged')

    # ------------------------------------------------------------------
    # IMAGE TESTS
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_explicit_image_held_in_pending(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        NSFW service returns high Porn score
        -> state should be held in 'pending' awaiting admin approval.
        """

        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        mock_requests_post.return_value.json.return_value = {
            'predictions': [
                {'className': 'Porn', 'probability': 0.91},
                {'className': 'Hentai', 'probability': 0.02},
                {'className': 'Sexy', 'probability': 0.05},
                {'className': 'Neutral', 'probability': 0.01},
                {'className': 'Drawing', 'probability': 0.01},
            ]
        }

        content = (
            '<p>Check this out</p>'
            '<img src="data:image/png;base64,aGVsbG8gd29ybGQ=">'
        )

        post = self._make_post('Image test post', content)
        self.assertEqual(post.state, 'pending')

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_safe_image_published(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        NSFW service returns a Neutral / Drawing dominant score
        -> should be published immediately ('active').
        """

        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        mock_requests_post.return_value.json.return_value = {
            'predictions': [
                {'className': 'Neutral', 'probability': 0.95},
                {'className': 'Porn', 'probability': 0.01},
                {'className': 'Hentai', 'probability': 0.01},
                {'className': 'Sexy', 'probability': 0.02},
                {'className': 'Drawing', 'probability': 0.01},
            ]
        }

        content = (
            '<p>Nice view today</p>'
            '<img src="data:image/png;base64,aGVsbG8gd29ybGQ=">'
        )

        post = self._make_post('Safe image test post', content)
        self.assertEqual(post.state, 'active')

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_safe_drawing_neutral_image_published(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        Image with Drawing and Neutral dominance (like test.png)
        -> should be published immediately ('active').
        """

        mock_predict.return_value = {
            'toxicity': 0.001,
            'severe_toxicity': 0.0,
            'obscene': 0.0,
            'threat': 0.0,
            'insult': 0.0,
            'identity_attack': 0.0,
        }

        mock_requests_post.return_value.json.return_value = {
            'predictions': [
                {'className': 'Drawing', 'probability': 0.598},
                {'className': 'Neutral', 'probability': 0.380},
                {'className': 'Hentai', 'probability': 0.021},
                {'className': 'Porn', 'probability': 0.0002},
                {'className': 'Sexy', 'probability': 0.0001},
            ]
        }

        content = (
            '<p>My drawing project</p>'
            '<img src="data:image/png;base64,aGVsbG8gd29ybGQ=">'
        )

        post = self._make_post('Drawing post', content)
        self.assertEqual(post.state, 'active')

    # ------------------------------------------------------------------
    # ROBUSTNESS / FAIL-OPEN TESTS
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_text_model_failure_does_not_crash_post_creation(
        self,
        mock_predict
    ):
        """
        If Detoxify throws an exception,
        post creation should still succeed and publish (fail-open).
        """

        mock_predict.side_effect = Exception(
            "model unavailable"
        )

        post = self._make_post(
            'Service down test',
            'Some normal text here.'
        )

        self.assertTrue(post.exists())
        self.assertEqual(post.state, 'active')

    @patch(
        'odoo.addons.forum_content_moderation.models.forum_post.requests.post'
    )
    @patch(
        'odoo.addons.forum_content_moderation.models.content_moderation_mixin.'
        '_text_model.predict'
    )
    def test_image_service_down_does_not_crash_post_creation(
        self,
        mock_predict,
        mock_requests_post
    ):
        """
        If the NSFW microservice is unreachable,
        post creation should still succeed and publish (fail-open).
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
        self.assertEqual(post.state, 'active')