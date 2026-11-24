# -*- coding: utf-8 -*-
{
    'name': 'Otomater Social Publisher',
    'version': '19.0.1.0.0',
    'category': 'Marketing',
    'summary': 'Publish posters and videos to Facebook Pages and Instagram with an approval workflow',
    'description': """
Social Publisher
================
* Facebook Page posts (text, photos, video) and Instagram (photo, carousel, Reels)
* Draft -> Submit -> Approve / Reject workflow before anything goes live
* Schedule posts, content calendar, per-account publishing results and retry
* Multi-account, multi-company, tokens visible to managers only
""",
    'author': 'Otomater',
    'website': 'https://otomater.com',
    'license': 'OPL-1',
    'depends': ['base', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'security/rules.xml',
        'data/sequence.xml',
        'data/cron.xml',
        'views/account_views.xml',
        'views/post_views.xml',
        'wizard/reject_wizard_views.xml',
        'views/menus.xml',
    ],
    'application': True,
    'installable': True,
}
