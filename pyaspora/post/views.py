import json
from flask import Blueprint, request, url_for
from sqlalchemy.sql import and_, not_

from pyaspora.content.models import MimePart
from pyaspora.content.rendering import render, renderer_exists
from pyaspora.contact.views import json_contact
from pyaspora.database import db
from pyaspora.post.models import Post, Share
from pyaspora.roster.views import json_group
from pyaspora.utils.rendering import abort, add_logged_in_user_to_data, \
    redirect, render_response
from pyaspora.utils.validation import check_attachment_is_safe, post_param
from pyaspora.user.session import require_logged_in_user
from pyaspora.tag.models import Tag
from pyaspora.tag.views import json_tag

blueprint = Blueprint('posts', __name__, template_folder='templates')


def json_post(post, viewing_as=None, share=None, children=True):
    """
    Turn a Post in sensible representation for serialisation, from the view of
    Contact 'viewing_as', or the public if not provided. If a Share is
    provided then additional actions are provided. If 'children' is False then
    child Posts of this Post will not be fetched.
    """
    sorted_parts = sorted(post.parts, key=lambda p: p.order)
    sorted_children = sorted(post.viewable_children(),
                             key=lambda p: p.created_at)
    data = {
        'id': post.id,
        'author': json_contact(post.author),
        'parts': [json_part(p) for p in sorted_parts],
        'children': None,
        'created_at': post.created_at.isoformat(),
        'actions': {
            'share': None,
            'comment': None,
            'hide': None,
            'make_public': None,
            'unmake_public': None,
        },
        'tags': [json_tag(t) for t in post.tags]
    }
    if children:
        data['children'] = [json_post(p, viewing_as, share)
                            for p in sorted_children]
    if viewing_as:
        data['actions']['comment'] = url_for('posts.comment',
                                             post_id=post.id, _external=True)
        if viewing_as.id != post.author_id:
            data['actions']['share'] = \
                url_for('posts.share', post_id=post.id, _external=True)

        if share and share.contact_id == viewing_as.id:
            data['actions']['hide'] = url_for('posts.hide',
                                              post_id=post.id, _external=True)
            if not post.parent:
                if share.public:
                    data['actions']['unmake_public'] = \
                        url_for('posts.set_public',
                                post_id=post.id, toggle='0', _external=True)
                else:
                    data['actions']['make_public'] = \
                        url_for('posts.set_public',
                                post_id=post.id, toggle='1', _external=True)
    return data


def json_part(part):
    """
    Turn a PostPart into a sensible format for serialisation.
    """
    url = url_for('content.raw', part_id=part.mime_part.id, _external=True)
    return {
        'inline': part.inline,
        'mime_type': part.mime_part.type,
        'text_preview': part.mime_part.text_preview,
        'link': url,
        'body': {
            'raw': str(part.mime_part.body) if part.inline else None,
            'text': render(part, 'text/plain', url),
            'html': render(part, 'text/html', url),
        }
    }


@blueprint.route('/<int:post_id>/share', methods=['GET'])
@require_logged_in_user
def share(post_id, _user):
    """
    Form to share an existing Post with more Contacts.
    """
    post = Post.get(post_id)
    if not post:
        abort(404, 'No such post', force_status=True)
    if not post.has_permission_to_view(_user.contact):
        abort(403, 'Forbidden')

    data = _base_create_form(_user)

    data.update({
        'relationship': {
            'type': 'share',
            'object': json_post(post, children=False),
            'description': 'Share this item'
        },
        'default_target': {
            'type': 'all_friends',
            'id': None
        }
    })
    return render_response('posts_create_form.tpl', data)


@blueprint.route('/<int:post_id>/comment', methods=['GET'])
@require_logged_in_user
def comment(post_id, _user):
    """
    Comment on (reply to) an existing Post.
    """
    post = Post.get(post_id)
    if not post:
        abort(404, 'No such post', force_status=True)
    if not post.has_permission_to_view(_user.contact):
        abort(403, 'Forbidden')

    data = _base_create_form(_user)

    data.update({
        'relationship': {
            'type': 'comment',
            'object': json_post(post, children=False),
            'description': 'Comment on this item'
        }
    })

    if post.author_id == _user.contact_id:
        data.update({
            'default_target': {
                'type': 'all_friends',
                'id': None
            }
        })
    else:
        data.update({
            'default_target': {
                'type': 'contact',
                'id': post.author_id
            }
        })

    return render_response('posts_create_form.tpl', data)


@require_logged_in_user
def _get_share_for_post(post_id, _user):
    share = db.session.query(Share).filter(and_(
        Share.contact == _user.contact,
        Share.post_id == post_id,
        not_(Share.hidden))).first()
    if not share:
        abort(403, 'Not available')

    return share


@blueprint.route('/<int:post_id>/hide', methods=['POST'])
def hide(post_id):
    """
    Hide an existing Post from the user's wall and profile.
    """
    share = _get_share_for_post(post_id)

    share.hidden = True
    db.session.add(share)
    db.session.commit()

    return redirect(url_for('feed.view', _external=True))


@blueprint.route('/<int:post_id>/set_public/<int:toggle>', methods=['POST'])
def set_public(post_id, toggle):
    """
    Make the Post appear-on/disappear-from the User's publiv wall. If toggle
    is True then the post will appear.
    """
    share = _get_share_for_post(post_id)

    if share.public != toggle:
        share.public = toggle
        db.session.add(share)
        db.session.commit()

    return redirect(url_for('feed.view', _external=True))


def _create_form_targets(user):
    data = [
        {
            'name': 'self',
            'description': 'Only visible to myself',
            'targets': None
        }
    ]
    user_list = [json_contact(c) for c in user.friends()]
    if user_list:
        data.append({
            'name': 'contact',
            'description': 'Share with one friend',
            'targets': user_list
        })

    group_list = user.groups
    if group_list:
        data.append({
            'name': 'group',
            'description': 'Share with a group of friends',
            'targets': [json_group(g, user) for g in group_list]
        })

    if user_list:
        data.append({
            'name': 'all_friends',
            'description': 'Share with all my friends',
            'targets': None
        })

    data.append(
        {
            'name': 'wall',
            'description': 'Show to everyone on my wall',
            'targets': None
        }
    )

    return data


def _base_create_form(user):
    data = {
        'next': url_for('.create', _external=True),
        'targets': _create_form_targets(user),
        'use_advanced_form': False
    }
    add_logged_in_user_to_data(data, user)
    return data


@blueprint.route('/create', methods=['GET'])
@require_logged_in_user
def create_form(_user):
    """
    Start a new Post.
    """
    data = _base_create_form(_user)
    data['use_advanced_form'] = True
    return render_response('posts_create_form.tpl', data)


@blueprint.route('/create', methods=['POST'])
@require_logged_in_user
def create(_user):
    """
    Create a new Post and Share it with the selected Contacts.
    """
    body = post_param('body')
    relationship = {
        'type': post_param('relationship_type', optional=True),
        'id': post_param('relationship_id', optional=True),
    }

    target = {
        'type': post_param('target_type'),
        'id': post_param('target_id', optional=True),
    }

    # Loathe inflexible HTML forms
    if target['id'] is None:
        target['id'] = post_param(
            'target_%s_id' % target['type'], optional=True)

    if relationship['type']:
        post = Post.get(relationship['id'])
        if not post:
            abort(404, 'No such post', force_status=True)
        if not post.has_permission_to_view(_user.contact):
            abort(403, 'Forbidden')
        relationship['post'] = post

    post = Post(author=_user.contact)
    body_part = MimePart(type='text/plain', body=body.encode('utf-8'),
                         text_preview=None)

    topics = post_param('tags', optional=True)
    if topics:
        post.tags = Tag.parse_line(topics, create=True)

    if relationship['type'] == 'comment':
        post.parent = relationship['post']
        post.add_part(body_part, order=0, inline=True)
    elif relationship['type'] == 'share':
        shared = relationship['post']
        share_part = MimePart(
            type='application/x-pyaspora-share',
            body=json.dumps({
                'post': {'id': shared.id},
                'author': {
                    'id': shared.author_id,
                    'name': shared.author.realname,
                }
            }).encode('utf-8'),
            text_preview="shared {}'s post".format(shared.author.realname)
        )
        post.add_part(share_part, order=0, inline=True)
        post.add_part(body_part, order=1, inline=True)
        order = 1
        for part in shared.parts:
            if part.mime_part.type != 'application/x-pyaspora-share':
                order += 1
                post.add_part(part.mime_part, inline=part.inline, order=order)
        if not post.tags:
            post.tags = shared.tags
    else:  # Naked post
        post.add_part(body_part, order=0, inline=True)
        attachment = request.files.get('attachment', None)
        if attachment:
            check_attachment_is_safe(attachment)
            attachment_part = MimePart(
                type=attachment.mimetype,
                body=attachment.stream.read(),
                text_preview=attachment.filename
            )
            post.add_part(attachment_part, order=1,
                          inline=bool(renderer_exists(attachment.mimetype)))

    db.session.add(post)

    post.share_with([_user.contact], show_on_wall=(target['type'] == 'wall'))
    if target['type'] == 'all_friends':
        for friend in _user.friends():
            post.share_with([friend])
    if target['type'] == 'contact':
        for friend in _user.friends():
            if str(friend.id) == target['id']:
                post.share_with([friend])
    if target['type'] == 'group':
        for group in _user.groups():
            if str(group.id) == target['id']:
                post.share_with([s.contact for s in group.subscriptions])

    db.session.commit()

    data = json_post(post)
    return redirect(url_for('feed.view', _external=True), data_structure=data)