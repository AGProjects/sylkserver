
from cassandra.cqlengine import columns
from cassandra.cqlengine.models import Model


class PushTokens(Model):
    __table_name__   = 'push_tokens'
    username         = columns.Text(partition_key=True)
    domain           = columns.Text(partition_key=True)
    device_id        = columns.Text(primary_key=True)
    app_id           = columns.Text(primary_key=True)
    background_token = columns.Text(required=False)
    device_token     = columns.Text()
    platform         = columns.Text()
    silent           = columns.Text()
    user_agent       = columns.Text(required=False)


class ChatMessage(Model):
    __table_name__  = 'chat_messages_by_timestamp'
    __options__     = {'default_time_to_live': '31536000',
                       'gc_grace_seconds': '345600'}
    account         = columns.Text(partition_key=True)
    contact         = columns.Text()
    created_at      = columns.DateTime(primary_key=True)
    direction       = columns.Text()
    message_id      = columns.Text(primary_key=True)
    content         = columns.Text()
    content_type    = columns.Text()
    disposition     = columns.List(value_type=columns.Text, required=False)
    state           = columns.Text()
    msg_timestamp   = columns.DateTime()


class ChatMessageIdMapping(Model):
    __table_name__  = 'chat_message_created_at_by_id'
    __options__     = {'default_time_to_live': '31536000',
                       'gc_grace_seconds': '345600'}
    message_id      = columns.Text(partition_key=True)
    created_at      = columns.DateTime()


class PublicKey(Model):
    __table_name__   = 'public_key_by_account'
    public_key       = columns.Text()
    account          = columns.Text(partition_key=True)
    created_at       = columns.DateTime()


class ChatAccount(Model):
    __table_name__   = 'chat_accounts'
    account          = columns.Text(partition_key=True)
    api_token        = columns.Text()
    last_login       = columns.DateTime()
