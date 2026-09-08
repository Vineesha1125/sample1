{
    'name': 'Forum Moderation Notice',
    'version': '1.0',
    'category': 'Website/Website',
    'author': 'Dealwallet',
    'summary': 'Shows a website notice when a forum post is blocked or flagged by moderation',
    'depends': ['website_forum', 'forum_content_moderation'],
    'data': [
        'views/moderation_notice_templates.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}