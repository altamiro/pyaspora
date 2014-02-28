from sqlalchemy.sql import and_

from pyaspora.database import db
from pyaspora.diaspora.models import DiasporaContact, MessageQueue
from pyaspora.diaspora.protocol import DiasporaMessageParser
from pyaspora.post.views import json_post
from pyaspora.roster.models import Subscription


def process_incoming_queue(user):
    from pyaspora.diaspora.actions import process_incoming_message

    # FIXME order by time received
    queue_items = db.session.query(MessageQueue).filter(
        and_(
            MessageQueue.format == MessageQueue.INCOMING,
            MessageQueue.local_user == user
        )
    )
    dmp = DiasporaMessageParser(DiasporaContact.get_by_username)
    for qi in queue_items:
        ret, c_from = dmp.decode(qi.body.decode('ascii'), user._unlocked_key)
        try:
            process_incoming_message(ret, c_from, user)
        except Exception:
            import traceback
            traceback.print_exc()
        else:
            db.session.delete(qi)
    db.session.commit()


def send_post(post, private):
    from pyaspora.diaspora.actions import PostMessage, PrivateMessage

    assert(post.author.user)

    self_share = [s for s in post.shares if post.author == s.contact][0]
    assert(self_share)

    # All people interested in the author
    targets = db.session.query(Subscription).filter(
        Subscription.to_contact == post.author
    )
    targets = [s.from_contact for s in targets if s.from_contact.diasp]
    if not self_share.public:
        shares = set([s.contact_id or s.contact.id for s in post.shares])
        targets = [c for c in targets if c.id in shares]

    json = json_post(post, children=False)
    text = "\n\n".join([p['body']['text'] for p in json['parts']])

    for target in targets:
        if private and not self_share.public:
            PrivateMessage.send(post.author.user, target,
                                post=post, text=text)
        else:
            PostMessage.send(post.author.user, target,
                             post=post, text=text, public=self_share.public)
