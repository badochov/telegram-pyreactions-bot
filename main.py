from typing import Optional
import logging
import json
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Updater,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
)
from db import get_conn
from collections import defaultdict
import emoji

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)

EMPTY_MSG = "\xad\xad"

EMOJI_CODES = {v for k, v in emoji.unicode_codes.EMOJI_ALIAS_UNICODE_ENGLISH.items()}


def split_into_chunks(l, n):
    return [l[i : i + n] for i in range(0, len(l), n)]


def char_is_emoji(character):
    # print(character, character in codes, 'test')
    return character in EMOJI_CODES


def get_markup(items):
    return InlineKeyboardMarkup(
        inline_keyboard=split_into_chunks(items, 4),
    )


def get_name_from_author_obj(data):
    username = data["username"]
    first_name = data["first_name"]
    return username or first_name


class MsgWrapper:
    def __init__(self, msg):
        self.msg = msg

    @property
    def is_reply(self) -> bool:
        return self.msg["reply_to_message"] is not None

    @property
    def msg_id(self) -> int:
        return self.msg["message_id"]

    @property
    def chat_id(self) -> int:
        return self.msg["chat"]["id"]

    @property
    def parent(self) -> Optional[int]:
        if self.is_reply:
            return self.msg["reply_to_message"]["message_id"]
        return None

    @property
    def is_reaction(self) -> bool:
        if self.text is None:
            return False

        return len(self.text) == 1 or self.text in ("+1", "-1", "xD")

    @property
    def is_many_reactions(self):
        return all(char_is_emoji(c) for c in self.text.strip())

    @property
    def text(self) -> str:
        if self.msg["text"].lower() == "xd":
            return "xD"
        return self.msg["text"].strip()

    @property
    def author(self) -> str:
        return get_name_from_author_obj(self.msg["from_user"])

    @property
    def author_id(self) -> str:
        return self.msg["from_user"]["id"]


def send_message(bot, chat_id: int, parent_id: int, markup) -> MsgWrapper:
    return MsgWrapper(
        bot.send_message(
            chat_id=chat_id,
            text=EMPTY_MSG,
            reply_markup=markup,
            reply_to_message_id=parent_id,
            parse_mode="HTML",
        )
    )


def save_message_to_db(msg: MsgWrapper, is_bot_reaction=False):
    print("saved", is_bot_reaction)
    sql = (
        "INSERT INTO message (id, chat_id, is_reply, parent, is_bot_reaction) \n"
        f"VALUES (?, ?, ?, ?, ?);"
    )
    with get_conn() as conn:
        conn.execute(
            sql, (msg.msg_id, msg.chat_id, msg.is_reply, msg.parent, is_bot_reaction)
        )


def get_updated_reactions(chat_id, parent_id):
    with get_conn() as conn:
        ret = conn.execute(
            "SELECT type, count(*) from reaction where parent=? group by type order by -count(*);",
            (parent_id,),
        )
        reactions = list(ret.fetchall())

    markup = [
        InlineKeyboardButton(
            f"{r[1]} {r[0]}️" if r[1] > 1 else r[0], callback_data=r[0]
        )
        for r in reactions
    ]

    with get_conn() as conn:
        reactions_post_id_opt = list(
            conn.execute(
                "SELECT id from message where is_bot_reaction and parent=?",
                (parent_id,),
            ).fetchall()
        )
        if reactions_post_id_opt:
            expanded = conn.execute(
                "SELECT expanded from message where id=?;",
                (reactions_post_id_opt[0][0],),
            ).fetchone()[0]
        else:
            expanded = False

    show_hide = "hide" if expanded else "show"
    markup.append(InlineKeyboardButton("❓", callback_data=show_hide + "_reactions"))

    print("markups: ", len(markup))
    return get_markup(markup)


def update_message_markup(bot, chat_id, message_id, parent_id):
    bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=get_updated_reactions(chat_id, parent_id),
    )


def get_text_for_expanded(parent):
    with get_conn() as conn:
        ret = conn.execute(
            "SELECT type, author from reaction where parent=?;",
            (parent,),
        )
        reactions = defaultdict(list)
        for r in ret.fetchall():
            reactions[r[0]].append(r[1])

    return "\n".join(
        key + ": " + ", ".join(reactioners) for key, reactioners in reactions.items()
    )


def add_delete_or_update_reaction_msg(bot, parent_id) -> None:
    with get_conn() as conn:
        ret = conn.execute("SELECT chat_id from message where id=?;", (parent_id,))
        chat_id = ret.fetchone()[0]

    with get_conn() as conn:
        opt_reactions_msg_id = list(
            conn.execute(
                "SELECT id, expanded from message where is_bot_reaction and parent=?",
                (parent_id,),
            ).fetchall()
        )

    reactions_markups = get_updated_reactions(chat_id, parent_id)

    if len(reactions_markups.inline_keyboard[0]) == 1:
        # removed last reaction
        with get_conn() as conn:
            conn.execute(
                "DELETE from message where is_bot_reaction and parent=?",
                (parent_id,),
            )
        bot.delete_message(chat_id=chat_id, message_id=opt_reactions_msg_id[0][0])
    elif not opt_reactions_msg_id:
        # adding new reactions msg
        new_msg = send_message(bot, chat_id, parent_id, reactions_markups)
        save_message_to_db(new_msg, is_bot_reaction=True)
    else:
        # updating existing reactions post
        # if expanded update text
        if opt_reactions_msg_id[0][1]:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=opt_reactions_msg_id[0][0],
                text=get_text_for_expanded(parent_id),
                parse_mode="HTML",
            )

        update_message_markup(bot, chat_id, opt_reactions_msg_id[0][0], parent_id)


def add_single_reaction(parent, author, author_id, text):
    with get_conn() as conn:
        ret = conn.execute(
            "SELECT id from reaction where parent=? and author_id=? and type=?;",
            (parent, author_id, text),
        )
        reaction_exists = list(ret.fetchall())

        if reaction_exists:
            print("deleting")
            conn.execute("DELETE from reaction where id=?;", (reaction_exists[0][0],))
        else:
            print("adding")
            sql = "INSERT INTO reaction (parent, author, type, author_id) VALUES (?, ?, ?, ?);"
            conn.execute(sql, (parent, author, text, author_id))

        conn.commit()


def toggle_reaction(bot, parent, author, text, author_id, many=False):
    if many:
        for r in text:
            add_single_reaction(parent, author, author_id, r)
    else:
        add_single_reaction(parent, author, author_id, text)

    add_delete_or_update_reaction_msg(bot, parent)


def receive_message(update: Update, context: CallbackContext) -> None:
    print("msg received")
    if update.edited_message:
        # skip edits
        return

    msg = MsgWrapper(update["message"])

    if not msg.is_reply or not (msg.is_reaction or msg.is_many_reactions):
        save_message_to_db(msg)
    else:
        parent = msg.parent

        # odpowiedz na wiadomosc bota ma aktualizowac parenta
        with get_conn() as conn:
            opt_parent = list(
                conn.execute(
                    "SELECT parent from message where is_bot_reaction and id=?",
                    (msg.parent,),
                ).fetchall()
            )
            if opt_parent:
                parent = opt_parent[0][0]

        if msg.is_many_reactions:
            toggle_reaction(
                context.bot,
                parent,
                msg.author,
                set(msg.text),
                msg.author_id,
                many=True,
            )
        else:
            toggle_reaction(context.bot, parent, msg.author, msg.text, msg.author_id)

        context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.msg_id)


def echo_photo(update: Update, context: CallbackContext) -> None:
    print("msg picture")
    save_message_to_db(MsgWrapper(update["message"]))


def show_hide_summary(bot, cmd, parent, reaction_post_id):
    with get_conn() as conn:
        chat_id = conn.execute(
            "SELECT chat_id from message where id=?;", (parent,)
        ).fetchone()[0]

    with get_conn() as conn:
        is_expanded = conn.execute(
            "select expanded from message where id=?;", (reaction_post_id,)
        ).fetchone()[0]

        if (cmd == "show_reactions" and is_expanded) or (
            cmd == "hide_reactions" and not is_expanded
        ):
            # cant show/hide already shown/hidden
            # race condition may produce multiple show/hide commands in a row
            return

    if cmd == "show_reactions":
        new_text = get_text_for_expanded(parent)

        with get_conn() as conn:
            conn.execute(
                "UPDATE message SET expanded=TRUE where id=?;", (reaction_post_id,)
            )
    else:
        with get_conn() as conn:
            conn.execute(
                "UPDATE message SET expanded=FALSE where id=?;", (reaction_post_id,)
            )
        new_text = EMPTY_MSG

    bot.edit_message_text(
        chat_id=chat_id, message_id=reaction_post_id, text=new_text, parse_mode="HTML"
    )
    update_message_markup(bot, chat_id, reaction_post_id, parent)


def button_callback_handler(update: Update, context: CallbackContext) -> None:
    callback_data = update["callback_query"]["data"]
    parent_msg = MsgWrapper(update["callback_query"]["message"])
    author = get_name_from_author_obj(update["callback_query"]["from_user"])
    author_id = update["callback_query"]["from_user"]["id"]

    print("button:", callback_data, author, update)

    if callback_data.endswith("reactions"):
        show_hide_summary(
            context.bot, callback_data, parent_msg.parent, parent_msg.msg_id
        )
    else:
        if len(callback_data) > 3:
            print("invalid reaction callback data")
            return

        toggle_reaction(
            context.bot,
            parent=parent_msg.parent,
            author=author,
            text=callback_data,
            author_id=author_id,
        )


def main() -> None:
    with open(".env") as f:
        token = json.loads(f.read())["token"]

    updater = Updater(token, workers=1)

    dispatcher = updater.dispatcher
    process_update = dispatcher.process_update

    def monkey_process_update(*args, **kwargs):
        print("processing update")
        process_update(*args, **kwargs)

    dispatcher.process_update = monkey_process_update

    dispatcher.add_handler(
        MessageHandler(Filters.text & ~Filters.command, receive_message)
    )
    dispatcher.add_handler(MessageHandler(Filters.photo, echo_photo))

    dispatcher.add_handler(
        CallbackQueryHandler(button_callback_handler, pattern="^.*$")
    )

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()