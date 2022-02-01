"""
Contains handler functions that handle commands received from a client, as well as appropriate errors.

Each command can include:
    - Source: Optional note of where the message came from, starting with ':'.
        * This is usually the server name or the user mask
    - Command: The specific command this message represents.
    - Parameters: Optional data relevant to this specific command – a series of values
        separated by one or more spaces. Parameters have different meanings for every single message.

    Ex:
        :Alice!AliceUsr@127.0.0.1  PRIVMSG  #foo :This is a message.

        |_______ SOURCE ________| |COMMAND| |_____ PARAMETERS ______|


All public functions start with "handle_".

To read how handler functions are called: see mantatail.recv_loop() documentation.
"""
from __future__ import annotations
import re
import mantatail
import irc_responses

from typing import Optional, Dict, List, Tuple


### Handlers
def handle_join(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command format: "JOIN #foo"

    If the channel already exists, the user is added to the channel.
    If the channel does not exist, the channel is created.

    Finally, sends a message to all users on the channel, notifying them that
    User has joined the channel.
    """

    if not args:
        error_not_enough_params(user, "JOIN")
        return

    channel_regex = r"#[^ \x07,]{1,49}"  # TODO: Make more restrictive (currently valid: ###, #ö?!~ etc)
    channel_name = args[0]
    lower_channel_name = channel_name.lower()

    if not re.match(channel_regex, lower_channel_name):
        error_no_such_channel(user, channel_name)
    else:
        if lower_channel_name not in state.channels.keys():
            state.channels[lower_channel_name] = mantatail.Channel(channel_name, user)

        channel = state.find_channel(channel_name)

        assert channel

        if user in channel.ban_list.keys():
            error_banned_from_chan(user, channel)
            return

        if user not in channel.users:
            channel_users_str = ""
            for usr in channel.users:
                channel_users_str += f" {usr.get_nick_with_prefix(channel)}"

            channel.users.add(user)

            join_msg = f"JOIN {channel_name}"
            channel.queue_message_to_chan_users(join_msg, user)

            if channel.topic:
                channel.send_topic_to_user(user)

            message = f"353 {user.nick} = {channel_name} :{user.get_nick_with_prefix(channel)}{channel_users_str}"
            user.send_que.put((message, "mantatail"))

            message = f"366 {user.nick} {channel_name} :End of /NAMES list."
            user.send_que.put((message, "mantatail"))

        # TODO:
        #   * Send topic (332)
        #   * Optional/Later: (333) https://modern.ircdocs.horse/#rpltopicwhotime-333
        #   * Forward to another channel (irc num 470) ex. #homebrew -> ##homebrew


def handle_part(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command format: "PART #foo"

    Removes user from a channel.

    Thereafter, sends a message to all users on the channel, notifying them that
    User has left the channel.
    """
    if not args:
        error_not_enough_params(user, "PART")
        return

    channel_name = args[0]

    channel = state.find_channel(channel_name)

    if not channel:
        error_no_such_channel(user, channel_name)
        return

    if user not in channel.users:
        error_not_on_channel(user, channel_name)
    else:
        channel.operators.discard(user)

        part_message = f"PART {channel_name}"
        channel.queue_message_to_chan_users(part_message, user)

        channel.users.discard(user)
        if len(channel.users) == 0:
            state.delete_channel(channel_name)


def handle_mode(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command format: "MODE #channel/user.nick +/-flag <args>"

    Sets a user/channel mode.

    Ex:
        - User mode "+i" makes user invisible
        - Channel mode "+i" makes channel invite-only.
        (Note: "+i" is not yet supported by Mantatail)
    """
    if not args:
        error_not_enough_params(user, "MODE")
        return

    if args[0].startswith("#"):
        process_channel_modes(state, user, args)
    else:
        target_usr = state.find_user(args[0])
        if not target_usr:
            error_no_such_channel(user, args[0])
            return
        else:
            if user != target_usr:
                # TODO: The actual IRC error for this should be "502 Can't change mode for other users"
                # This will be implemented when MODE becomes more widely supported.
                # Currently not sure which modes 502 applies to.
                error_no_such_channel(user, args[0])
                return
        process_user_modes()


def handle_nick(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Sets a user's nickname if they don't already have one.
    Changes the user's nickname if they already have one.
    """
    nick_regex = r"[a-zA-Z|\\_\[\]{}^`-][a-zA-Z0-9|\\_\[\]{}^`-]{,15}"

    if not args:
        error_no_nickname_given(user)
        return

    new_nick = args[0]
    if not re.fullmatch(nick_regex, new_nick):
        error_erroneus_nickname(user, new_nick)
        return
    elif new_nick in state.connected_users.keys():
        error_nick_in_use(user, new_nick)
    else:
        if user.nick == "*":
            user.nick = new_nick
            state.connected_users[user.nick.lower()] = user
        else:
            if new_nick == user.nick:
                return
            # Avoids sending NICK message to users several times if user shares more than one channel with them.
            receivers = user.get_users_sharing_channel()
            message = f"NICK :{new_nick}"

            for receiver in receivers:
                receiver.send_que.put((message, user.get_user_mask()))

            # User doesn't get NICK message if they change their nicks before sending USER command
            if user.user_message:
                user.send_que.put((message, user.get_user_mask()))

            # Not using state.delete_user() as that will delete the user from all channels as well.
            del state.connected_users[user.nick.lower()]

            user.nick = new_nick
            state.connected_users[user.nick.lower()] = user


def handle_away(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command formats:
        Set away status "AWAY :Away message"
        Remove away status "AWAY"

    Sets/Removes the Away status of a user. If somebody sends a PRIVMSG to a user who is Away,
    they will receive a reply with the user's away message.
    """

    # args[0] == "" happens when user sends "AWAY :", which indicates they are no longer away.
    if not args or args[0] == "":
        (unaway_num, unaway_info) = irc_responses.RPL_UNAWAY
        unaway_message = f"{unaway_num} {user.nick} {unaway_info}"
        user.send_que.put((unaway_message, "mantatail"))
        user.away = None
    else:
        (nowaway_num, nowaway_info) = irc_responses.RPL_NOWAWAY
        nowaway_message = f"{nowaway_num} {user.nick} {nowaway_info}"
        user.send_que.put((nowaway_message, "mantatail"))
        user.away = args[0]


def handle_topic(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command formats:
        Set new topic: "TOPIC #foo :New Topic"
        Clear topic: "TOPIC #foo :"
        Get topic: "TOPIC #foo"

    Depending on command and operator status, either sends a channel's topic to user, sets a new topic,
    or clears the current topic.
    """
    if not args:
        error_not_enough_params(user, "TOPIC")
        return

    channel = state.find_channel(args[0])

    if not channel:
        error_no_such_channel(user, args[0])
        return

    if len(args) == 1:
        channel.send_topic_to_user(user)
    else:
        if not user in channel.operators:
            error_no_operator_privileges(user, channel)
        else:
            channel.set_topic(user, args[1])

            if not args[1]:
                topic_message = f"TOPIC {channel.name} :"
            else:
                topic_message = f"TOPIC {channel.name} :{args[1]}"

            channel.queue_message_to_chan_users(topic_message, user)


def handle_kick(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command format: "KICK #foo user_to_kick (:Reason for kicking)"

    Kicks a user from a channel. The kicker must be an operator on that channel.

    Notifies the kicked user that they have been kicked and the reason for it.
    Thereafter, sends a message to all users on the channel, notifying them
    that an operator has kicked a user.
    """
    if not args or len(args) == 1:
        error_not_enough_params(user, "KICK")
        return

    channel = state.find_channel(args[0])
    if not channel:
        error_no_such_channel(user, args[0])
        return

    target_usr = state.find_user(args[1])
    if not target_usr:
        error_no_such_nick_channel(user, args[1])
        return

    if user not in channel.operators:
        error_no_operator_privileges(user, channel)
        return

    if target_usr not in channel.users:
        error_user_not_in_channel(user, target_usr, channel)
        return

    if len(args) == 2:
        kick_message = f"KICK {channel.name} {target_usr.nick} :{target_usr.nick}"
    elif len(args) >= 3:
        reason = args[2]
        kick_message = f"KICK {channel.name} {target_usr.nick} :{reason}"

    channel.queue_message_to_chan_users(kick_message, user)
    channel.users.discard(target_usr)
    channel.operators.discard(target_usr)

    if len(channel.users) == 0:
        state.delete_channel(channel.name)


def handle_quit(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command format: "QUIT"

    Disconnects a user from the server by putting tuple (None, disconnect_reason: str) to their send queue.
    """
    if args:
        disconnect_reason = args[0]
    else:
        disconnect_reason = "Client quit"

    user.send_que.put((None, disconnect_reason))


def handle_privmsg(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Command format: "PRIVMSG #channel/user.nick :This is a message"

    Depending on the command, sends a message to all users on a channel or a private message to a user.
    """

    # TODO: Check if user is in channel ban list

    if not args:
        error_no_recipient(user, "PRIVMSG")
        return
    elif len(args) == 1:
        error_no_text_to_send(user)
        return

    (receiver, privmsg) = args[0], args[1]

    if receiver.startswith("#"):

        channel = state.find_channel(receiver)
        if not channel:
            error_no_such_channel(user, receiver)
            return
    else:
        privmsg_to_user(state, user, receiver, privmsg)
        return

    if user not in channel.users:
        error_not_on_channel(user, receiver)
    elif user in channel.ban_list.keys():
        error_cannot_send_to_channel(user, channel.name)
    else:
        privmsg_message = f"PRIVMSG {receiver} :{privmsg}"
        channel.queue_message_to_chan_users(privmsg_message, user, send_to_self=False)


def handle_pong(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Handles client's PONG response to a PING message sent from the server.

    The PONG message notifies the server that the client still has an open connection to it.

    The parameter sent in the PONG message must correspond to the parameter in the PING message.
    Ex.
        PING :This_is_a_parameter
        PONG :This_is_a_parameter
    """
    if args and args[0] == "mantatail":
        user.pong_received = True
    else:
        error_no_origin(user)


# Private functions
def privmsg_to_user(
    state: mantatail.ServerState, sender: mantatail.UserConnection, receiver: str, privmsg: str
) -> None:
    receiver_usr = state.find_user(receiver)
    if not receiver_usr:
        error_no_such_nick_channel(sender, receiver)
        return

    message = f"PRIVMSG {receiver_usr.nick} :{privmsg}"
    receiver_usr.send_que.put((message, sender.get_user_mask()))

    if receiver_usr.away:
        away_num = irc_responses.RPL_AWAY
        away_message = f"{away_num} {sender.nick} {receiver_usr.nick} :{receiver_usr.away}"
        sender.send_que.put((away_message, "mantatail"))


def motd(motd_content: Optional[Dict[str, List[str]]], user: mantatail.UserConnection) -> None:
    """
    Sends the server's Message of the Day to the user.

    This is sent to a user when they have registered a nick and a username on the server.
    """
    (start_num, start_info) = irc_responses.RPL_MOTDSTART
    motd_num = irc_responses.RPL_MOTD
    (end_num, end_info) = irc_responses.RPL_ENDOFMOTD

    motd_start_and_end = {
        "start_msg": f"{start_num} {user.nick} :- mantatail {start_info}",
        "end_msg": f"{end_num} {user.nick} {end_info}",
    }

    user.send_que.put((motd_start_and_end["start_msg"], "mantatail"))

    if motd_content:
        motd = motd_content["motd"]
        for motd_line in motd:
            motd_message = f"{motd_num} {user.nick} :{motd_line.format(user_nick=user.nick)}"
            user.send_que.put((motd_message, "mantatail"))
    # If motd.json could not be found
    else:
        error_no_motd(user)

    user.send_que.put((motd_start_and_end["end_msg"], "mantatail"))


def process_channel_modes(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    """
    Given that the user has the required privileges, sets the requested channel mode.

    Ex. Make a channel invite-only, or set a channel operator.

    Finally sends a message to all users on the channel, notifying them about the new channel mode.
    """
    channel = state.find_channel(args[0])
    if not channel:
        error_no_such_channel(user, args[0])
        return

    if len(args) == 1:
        if channel.modes:
            message = f'{irc_responses.RPL_CHANNELMODEIS} {user.nick} {channel.name} +{" ".join(channel.modes)}'
        else:
            message = f"{irc_responses.RPL_CHANNELMODEIS} {user.nick} {channel.name}"
        user.send_que.put((message, "mantatail"))
    else:
        if args[1][0] not in ["+", "-"]:
            error_unknown_mode(user, args[1][0])
            return

        valid_chanmodes = r"[a-zA-Z]"
        supported_modes = [chanmode for chanmodes in state.chanmodes.values() for chanmode in chanmodes]

        for mode in args[1][1:]:
            if mode not in supported_modes or not re.fullmatch(valid_chanmodes, mode):
                error_unknown_mode(user, mode)
                return

        mode_command, flags = args[1][0], args[1][1:]
        parameters = iter(args[2:])
        for flag in flags:

            if flag == "o":
                current_param = next(parameters, None)

                process_mode_o(state, user, channel, mode_command, current_param)

            elif flag == "b":
                current_param = next(parameters, None)

                process_mode_b(state, user, channel, mode_command, current_param)


def process_mode_b(
    state: mantatail.ServerState,
    user: mantatail.UserConnection,
    channel: mantatail.Channel,
    mode_command: str,
    ban_target: Optional[str],
) -> None:
    """Bans or unbans a user from a channel."""
    if not ban_target:
        if channel.ban_list:
            banlist_num = irc_responses.RPL_BANLIST

            for usr, banner in channel.ban_list.items():
                message = f"{banlist_num} {user.nick} {channel.name} {usr.nick}!*@* {banner}"
                user.send_que.put((message, "mantatail"))

        (endbanlist_num, endbanlist_info) = irc_responses.RPL_ENDOFBANLIST
        message = f"{endbanlist_num} {user.nick} {channel.name} {endbanlist_info}"
        user.send_que.put((message, "mantatail"))
        return

    # target_usr = state.find_user(target_usr_nick)

    # if not target_usr:
    #     error_no_such_nick_channel(user, target_usr_nick)
    #     return
    if user not in channel.operators:
        error_no_operator_privileges(user, channel)
        return

    generate_ban_mask(ban_target)

    mode_message = f"MODE {channel.name} {mode_command}b {target_usr.nick}!*@*"

    banned_users = channel.ban_list.keys()

    # Not sending message if "+b" and target usr is already banned (or vice versa)
    if mode_command == "+" and target_usr not in banned_users:
        channel.queue_message_to_chan_users(mode_message, user)
        channel.ban_list[target_usr] = f"{user.get_user_mask()}"

    elif mode_command[0] == "-" and target_usr in banned_users:
        channel.queue_message_to_chan_users(mode_message, user)
        del channel.ban_list[target_usr]


def process_mode_o(
    state: mantatail.ServerState,
    user: mantatail.UserConnection,
    channel: mantatail.Channel,
    mode_command: str,
    target_usr_nick: Optional[str],
) -> None:
    """Sets or removes channel operator"""
    if not target_usr_nick:
        error_not_enough_params(user, "MODE")
        return

    target_usr = state.find_user(target_usr_nick)

    if not target_usr:
        error_no_such_nick_channel(user, target_usr_nick)
        return
    if user not in channel.operators:
        error_no_operator_privileges(user, channel)
        return
    if target_usr not in channel.users:
        error_user_not_in_channel(user, target_usr, channel)
        return

    mode_message = f"MODE {channel.name} {mode_command}o {target_usr.nick}"

    if mode_command == "+" and target_usr not in channel.operators:
        channel.queue_message_to_chan_users(mode_message, user)
        channel.operators.add(target_usr)

    elif mode_command[0] == "-" and target_usr in channel.operators:
        channel.queue_message_to_chan_users(mode_message, user)
        channel.operators.discard(target_usr)


# !Not implemented
def process_user_modes() -> None:
    pass


def parse_received_args(msg: str) -> Tuple[str, List[str]]:
    """
    Parses the user command by separating the command (e.g "join", "privmsg", etc.) from the
    arguments.

    If a parameter contains spaces, it must start with ':' to be interpreted as one parameter.
    If the parameter does not start with ':', it will be cut off at the first space.

    Ex:
        - "PRIVMSG #foo :This is a message\r\n" will send "This is a message"
        - "PRIVMSG #foo This is a message\r\n" will send "This"
    """
    split_msg = msg.split(" ")

    for num, arg in enumerate(split_msg):
        if arg.startswith(":"):
            parsed_msg = split_msg[:num]
            parsed_msg.append(" ".join(split_msg[num:])[1:])
            command = parsed_msg[0]
            return command, parsed_msg[1:]

    command = split_msg[0]
    return command, split_msg[1:]


def generate_ban_mask(ban_target: str) -> str:
    """ """
    if "!" in ban_target and "@" in ban_target:
        ban_mask_regex = r"(.*)!(.*)@(.*)"
        ban_match = re.fullmatch(ban_mask_regex, ban_target)
        if not ban_match:
            # @ before ! (corner case)
            pass
        else:
            nick, user, host = ban_match.groups()

    elif "!" in ban_target:
        nick, user, host = "!".split(ban_target, 1)

    elif "@" in ban_target:
        nick, user, host = "@".split(ban_target, 1)

    #
    # if "!" in ban_target and "@" in ban_target:
    #   regex for *!*@*
    #   regex_groups_list = blah.groups()
    # elif "!" in ban_target:
    #   regex for *!*
    #   regex_groups_list = blah.groups()
    # elif "@" in ban_target:
    #   regex for *@*
    #   regex_groups_list = blah.groups()
    # else:
    #   regex for *
    #   regex_groups_list = blah.groups()
    #
    # final_mask = []
    #
    # for x in regex_groups_list:
    #   if not x:
    #     final_mask.append correct thingy (! or @)
    #   else:
    #     final_mask.append x

    pass


### Error Messages
def error_unknown_command(user: mantatail.UserConnection, command: str) -> None:
    """Sent when server does not recognize a command user sent to server."""
    (unknown_cmd_num, unknown_cmd_info) = irc_responses.ERR_UNKNOWNCOMMAND

    message = f"{unknown_cmd_num} {user.nick} {command} {unknown_cmd_info}"
    user.send_que.put((message, "mantatail"))


def error_not_registered(user: mantatail.UserConnection) -> None:
    """
    Sent when a user sends a command before registering to the server.
    Registering is done with commands NICK & USER.
    """
    (not_registered_num, not_registered_info) = irc_responses.ERR_NOTREGISTERED

    message = f"{not_registered_num} {user.nick} {not_registered_info}"
    user.send_que.put((message, "mantatail"))


def error_no_motd(user: mantatail.UserConnection) -> None:
    """Sent when server cannot find the Message of the Day."""
    (no_motd_num, no_motd_info) = irc_responses.ERR_NOMOTD

    message = f"{no_motd_num} {user.nick} {no_motd_info}"
    user.send_que.put((message, "mantatail"))


def error_erroneus_nickname(user: mantatail.UserConnection, new_nick: str) -> None:
    (err_nick_num, err_nick_info) = irc_responses.ERR_ERRONEUSNICKNAME

    message = f"{err_nick_num} {new_nick} {err_nick_info}"
    user.send_que.put((message, "mantatail"))


def error_nick_in_use(user: mantatail.UserConnection, nick: str) -> None:
    """Sent when a Nick that a user tries to establish is already in use."""
    (nick_in_use_num, nick_in_use_info) = irc_responses.ERR_NICKNAMEINUSE

    message = f"{nick_in_use_num} {user.nick} {nick} {nick_in_use_info}"
    user.send_que.put((message, "mantatail"))


def error_no_nickname_given(user: mantatail.UserConnection) -> None:
    (no_nick_given_num, no_nick_given_info) = irc_responses.ERR_NONICKNAMEGIVEN

    message = f"{no_nick_given_num} {no_nick_given_info}"
    user.send_que.put((message, "mantatail"))


def error_no_such_nick_channel(user: mantatail.UserConnection, channel_or_nick: str) -> None:
    """Sent when a user provides a non-existing user or channel as an argument in a command."""
    (no_nick_num, no_nick_info) = irc_responses.ERR_NOSUCHNICK

    message = f"{no_nick_num} {user.nick} {channel_or_nick} {no_nick_info}"
    user.send_que.put((message, "mantatail"))


def error_not_on_channel(user: mantatail.UserConnection, channel_name: str) -> None:
    """Sent when a user tries to send a message to, or part from a channel that they are not connected to."""
    (not_on_channel_num, not_on_channel_info) = irc_responses.ERR_NOTONCHANNEL

    message = f"{not_on_channel_num} {user.nick} {channel_name} {not_on_channel_info}"
    user.send_que.put((message, "mantatail"))


def error_user_not_in_channel(
    user: mantatail.UserConnection, target_usr: mantatail.UserConnection, channel: mantatail.Channel
) -> None:
    """
    Sent when a user sends a channel-specific command with a user as an argument,
    and this user is connected to the server but has not joined the channel.
    """
    (not_in_chan_num, not_in_chan_info) = irc_responses.ERR_USERNOTINCHANNEL
    message = f"{not_in_chan_num} {user.nick} {target_usr.nick} {channel.name} {not_in_chan_info}"
    user.send_que.put((message, "mantatail"))


def error_cannot_send_to_channel(user: mantatail.UserConnection, channel_name: str) -> None:
    """
    Sent when privmsg/notice cannot be sent to channel.

    This is generally sent in response to channel modes, such as a channel being moderated
    and the client not having permission to speak on the channel, or not being joined to
    a channel with the no external messages mode set.
    """
    (cant_send_num, cant_send_info) = irc_responses.ERR_CANNOTSENDTOCHAN

    message = f"{cant_send_num} {user.nick} {channel_name} {cant_send_info}"
    user.send_que.put((message, "mantatail"))


def error_banned_from_chan(user: mantatail.UserConnection, channel: mantatail.Channel) -> None:
    """Notifies the user trying to join a channel that they are banned from that channel."""
    (banned_num, banned_info) = irc_responses.ERR_BANNEDFROMCHAN
    message = f"{banned_num} {user.nick} {channel.name} {banned_info}"
    user.send_que.put((message, "mantatail"))


def error_no_such_channel(user: mantatail.UserConnection, channel_name: str) -> None:
    """Sent when a user provides a non-existing channel as an argument in a command."""
    (no_channel_num, no_channel_info) = irc_responses.ERR_NOSUCHCHANNEL
    message = f"{no_channel_num} {user.nick} {channel_name} {no_channel_info}"
    user.send_que.put((message, "mantatail"))


def error_no_operator_privileges(user: mantatail.UserConnection, channel: mantatail.Channel) -> None:
    """
    Sent when a user is trying to perform an action reserved to channel operators,
    but is not an operator on that channel.
    """
    (not_operator_num, not_operator_info) = irc_responses.ERR_CHANOPRIVSNEEDED
    message = f"{not_operator_num} {user.nick} {channel.name} {not_operator_info}"
    user.send_que.put((message, "mantatail"))


def error_no_recipient(user: mantatail.UserConnection, command: str) -> None:
    """Sent when a user sends a PRIVMSG but without providing a recipient."""
    (no_recipient_num, no_recipient_info) = irc_responses.ERR_NORECIPIENT

    message = f"{no_recipient_num} {user.nick} {no_recipient_info} ({command.upper()})"
    user.send_que.put((message, "mantatail"))


def error_no_text_to_send(user: mantatail.UserConnection) -> None:
    """
    Sent when a user tries to send a PRIVMSG but without providing any message to send.
    Ex. "PRIVMSG #foo"
    """
    (no_text_num, no_text_info) = irc_responses.ERR_NOTEXTTOSEND

    message = f"{no_text_num} {user.nick} {no_text_info}"
    user.send_que.put((message, "mantatail"))


def error_unknown_mode(user: mantatail.UserConnection, unknown_command: str) -> None:
    """Sent when a user tries to set a channel/user mode that the server does not recognize."""
    (unknown_mode_num, unknown_mode_info) = irc_responses.ERR_UNKNOWNMODE
    message = f"{unknown_mode_num} {user.nick} {unknown_command} {unknown_mode_info}"
    user.send_que.put((message, "mantatail"))


def error_no_origin(user: mantatail.UserConnection) -> None:
    """
    Sent when the argument of a PONG message sent as a response to the server's
    PING message does not correspond to the argument sent in the PING message.
    """
    (no_origin_num, no_origin_info) = irc_responses.ERR_NOORIGIN

    message = f"{no_origin_num} {user.nick} {no_origin_info}"
    user.send_que.put((message, "mantatail"))


def error_not_enough_params(user: mantatail.UserConnection, command: str) -> None:
    """Sent when a user sends a command to the server that does not contain all required arguments."""
    (not_enough_params_num, not_enough_params_info) = irc_responses.ERR_NEEDMOREPARAMS
    message = f"{not_enough_params_num} {user.nick} {command} {not_enough_params_info}"
    user.send_que.put((message, "mantatail"))
