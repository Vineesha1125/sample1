{
    'name': 'Forum AI Content Moderation',
    'version': '1.0',
    'depends': ['website_forum'],
    'author': 'You',
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'data': [
        'security/ir.model.access.csv',
        'views/forum_moderation_views.xml',
    ],
}