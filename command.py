from __future__ import annotations
import re
import mantatail
import irc_responses

from typing import Optional, Dict, List


### Handlers
def handle_join(state: mantatail.ServerState, user: mantatail.UserConnection, channel_name: str) -> None:
    channel_regex = r"#[^ \x07,]{1,49}"  # TODO: Make more restrictive (currently valid: ###, #ö?!~ etc)
    lower_channel_name = channel_name.lower()

    if not re.match(channel_regex, lower_channel_name):
        error_no_such_channel(user, channel_name)
    else:
        if lower_channel_name not in state.channels.keys():
            state.channels[lower_channel_name] = mantatail.Channel(channel_name, user)

        channel = state.channels[lower_channel_name]

        if user not in channel.users:
            channel_users_str = ""
            for usr in channel.users:
                if usr.user_name == channel.founder:
                    nick = f"~{usr.nick}"
                elif channel.is_operator(usr):
                    nick = f"@{usr.nick}"
                else:
                    nick = usr.nick
                channel_users_str += f" {nick}"

            channel.users.add(user)

            for usr in channel.users:
                message = f"JOIN {channel_name}"
                usr.send_string_to_client(message, prefix=user.user_mask)

            # TODO: Implement topic functionality for existing channels & MODE for new ones

            message = f"353 {user.nick} = {channel_name} :{user.nick} {channel_users_str.strip()}"
            user.send_string_to_client(message)

            message = f"366 {user.nick} {channel_name} :End of /NAMES list."
            user.send_string_to_client(message)

        # TODO:
        #   * Send topic (332)
        #   * Optional/Later: (333) https://modern.ircdocs.horse/#rpltopicwhotime-333
        #   * Forward to another channel (irc num 470) ex. #homebrew -> ##homebrew


def handle_part(state: mantatail.ServerState, user: mantatail.UserConnection, channel_name: str) -> None:
    try:
        channel = state.find_channel(channel_name)
    except KeyError:
        error_no_such_channel(user, channel_name)
        return

    if user not in channel.users:
        error_not_on_channel(user, channel_name)
    else:
        if channel.is_operator(user):
            channel.remove_operator(user)

        for usr in channel.users:
            message = f"PART {channel_name}"
            usr.send_string_to_client(message, prefix=user.user_mask)

        channel.users.discard(user)
        if len(channel.users) == 0:
            state.delete_channel(channel_name)


def handle_mode(state: mantatail.ServerState, user: mantatail.UserConnection, mode_args: str) -> None:
    args = mode_args.split(" ")

    if args[0].startswith("#"):
        process_channel_modes(state, user, args)
    else:
        process_user_modes()


def handle_kick(state: mantatail.ServerState, user: mantatail.UserConnection, arg: str) -> None:
    args = arg.split(" ")

    if len(args) == 1:
        error_not_enough_params(user, "KICK")
        return
    try:
        channel = state.find_channel(args[0])
    except KeyError:
        error_no_such_channel(user, args[0])
        return
    try:
        target_usr = state.find_user(args[1])
    except KeyError:
        error_no_such_nick_channel(user, args[1])
        return

    if not channel.is_operator(user):
        error_no_operator_privileges(user, state.find_channel(args[0]))
        return

    if target_usr not in channel.users:
        error_user_not_in_channel(user, target_usr, channel)
        return

    if len(args) == 2:
        message = f"KICK {channel.name} {target_usr.nick} :{target_usr.nick}\r\n"
    elif len(args) >= 3:
        if not args[2].startswith(":"):
            reason = f":{args[2]}"
        else:
            reason = " ".join(args[2:])
        message = f"KICK {channel.name} {target_usr.nick} {reason}\r\n"
    channel.kick_user(user, target_usr, message)


def handle_quit(state: mantatail.ServerState, user: mantatail.UserConnection, command: str) -> None:
    # TODO: Implement logic for different reasons & disconnects.
    reason = "(Remote host closed the connection)"
    message = f"QUIT :Quit: {reason}"

    receivers = set()
    receivers.add(user)
    for channel in state.channels.values():
        if user in channel.users:
            for usr in channel.users:
                receivers.add(usr)
            channel.users.discard(user)

        if channel.is_operator(user):
            channel.remove_operator(user)

    for receiver in receivers:
        receiver.send_string_to_client(message, prefix=user.user_mask)

    state.delete_user(user.nick)

    user.closed_connection = True
    user.socket.close()


def handle_privmsg(state: mantatail.ServerState, user: mantatail.UserConnection, msg: str) -> None:
    (receiver, colon_privmsg) = msg.split(" ", 1)
    assert colon_privmsg.startswith(":")

    if receiver.startswith("#"):
        try:
            channel = state.find_channel(receiver)
        except KeyError:
            error_no_such_nick_channel(user, receiver)
            return
    else:
        privmsg_to_user(receiver, colon_privmsg)
        return

    if user not in channel.users:
        error_not_on_channel(user, receiver)
    else:
        for usr in channel.users:
            if usr.nick != user.nick:
                message = f"PRIVMSG {receiver} {colon_privmsg}"
                usr.send_string_to_client(message, prefix=user.user_mask)


# Private functions

# !Not implemented
def privmsg_to_user(receiver: str, colon_privmsg: str) -> None:
    pass


def motd(motd_content: Optional[Dict[str, List[str]]], user: mantatail.UserConnection) -> None:
    (start_num, start_info) = irc_responses.RPL_MOTDSTART
    motd_num = irc_responses.RPL_MOTD
    (end_num, end_info) = irc_responses.RPL_ENDOFMOTD

    motd_start_and_end = {
        "start_msg": f"{start_num} {user.nick} :- mantatail {start_info}",
        "end_msg": f"{end_num} {user.nick} {end_info}",
    }

    user.send_string_to_client(motd_start_and_end["start_msg"])

    if motd_content:
        motd = motd_content["motd"]
        for motd_line in motd:
            motd_message = f"{motd_num} {user.nick} :{motd_line.format(user_nick=user.nick)}"
            user.send_string_to_client(motd_message)
    # If motd.json could not be found
    else:
        error_no_motd(user)

    user.send_string_to_client(motd_start_and_end["end_msg"])


def process_channel_modes(state: mantatail.ServerState, user: mantatail.UserConnection, args: List[str]) -> None:
    if args[1][0] not in ["+", "-"]:
        error_unknown_mode(user, args[1][0])
        return
    supported_modes = ["o"]
    for mode in args[1][1:]:
        if mode not in supported_modes:
            error_unknown_mode(user, mode)
            return

    try:
        channel = state.find_channel(args[0])
    except KeyError:
        error_no_such_channel(user, args[0])
        return

    if len(args) == 1:
        message = f'{irc_responses.RPL_CHANNELMODEIS} {channel.name} {" ".join(channel.modes)}'
        user.send_string_to_client(message)
    elif len(args) == 2:
        error_not_enough_params(user, "MODE")
    else:
        mode_command, flags = args[1][0], args[1][1:]
        try:
            target_usr = state.find_user(args[2])
        except KeyError:
            error_no_such_nick_channel(user, args[2])
            return

        for flag in flags:
            if flag == "o":
                if not channel.is_operator(user):
                    error_no_operator_privileges(user, channel)
                    return
                elif target_usr not in channel.users:
                    error_user_not_in_channel(user, target_usr, channel)
                    return

                if mode_command == "+":
                    channel.set_operator(target_usr)
                elif mode_command[0] == "-":
                    channel.remove_operator(target_usr)

                message = f"MODE {channel.name} {mode_command}o {target_usr.nick}"
                for usr in channel.users:
                    usr.send_string_to_client(message)


# !Not implemented
def process_user_modes() -> None:
    pass


### Error Messages
def error_unknown_command(user: mantatail.UserConnection, command: str) -> None:
    (unknown_cmd_num, unknown_cmd_info) = irc_responses.ERR_UNKNOWNCOMMAND

    message = f"{unknown_cmd_num} {command} {unknown_cmd_info}"
    user.send_string_to_client(message)


def error_not_registered() -> bytes:
    (not_registered_num, not_registered_info) = irc_responses.ERR_NOTREGISTERED

    return bytes(f":mantatail {not_registered_num} * {not_registered_info}\r\n", encoding="utf-8")


def error_no_motd(user: mantatail.UserConnection) -> None:
    (no_motd_num, no_motd_info) = irc_responses.ERR_NOMOTD

    message = f"{no_motd_num} {no_motd_info}"
    user.send_string_to_client(message)


def error_nick_in_use(nick: str) -> bytes:
    (nick_in_use_num, nick_in_use_info) = irc_responses.ERR_NICKNAMEINUSE

    return bytes(f":mantatail {nick_in_use_num} {nick} {nick_in_use_info}\r\n", encoding="utf-8")


def error_no_such_nick_channel(user: mantatail.UserConnection, channel_or_nick: str) -> None:
    (no_nick_num, no_nick_info) = irc_responses.ERR_NOSUCHNICK

    message = f"{no_nick_num} {channel_or_nick} {no_nick_info}"
    user.send_string_to_client(message)


def error_not_on_channel(user: mantatail.UserConnection, channel_name: str) -> None:
    (not_on_channel_num, not_on_channel_info) = irc_responses.ERR_NOTONCHANNEL

    message = f"{not_on_channel_num} {channel_name} {not_on_channel_info}"
    user.send_string_to_client(message)


def error_user_not_in_channel(
    user: mantatail.UserConnection, target_usr: mantatail.UserConnection, channel: mantatail.Channel
) -> None:
    (not_in_chan_num, not_in_chan_info) = irc_responses.ERR_USERNOTINCHANNEL
    message = f"{not_in_chan_num} {target_usr.nick} {channel.name} {not_in_chan_info}"
    user.send_string_to_client(message)


def error_cannot_send_to_channel(user: mantatail.UserConnection, channel_name: str) -> None:
    (cant_send_num, cant_send_info) = irc_responses.ERR_CANNOTSENDTOCHAN

    message = f"{cant_send_num} {channel_name} {cant_send_info}"
    user.send_string_to_client(message)


def error_no_such_channel(user: mantatail.UserConnection, channel_name: str) -> None:
    (no_channel_num, no_channel_info) = irc_responses.ERR_NOSUCHCHANNEL
    message = f"{no_channel_num} {channel_name} {no_channel_info}"
    user.send_string_to_client(message)


def error_no_operator_privileges(user: mantatail.UserConnection, channel: mantatail.Channel) -> None:
    (not_operator_num, not_operator_info) = irc_responses.ERR_CHANOPRIVSNEEDED
    message = f"{not_operator_num} {channel.name} {not_operator_info}"
    user.send_string_to_client(message)


def error_unknown_mode(user: mantatail.UserConnection, unknown_command: str) -> None:
    (unknown_mode_num, unknown_mode_info) = irc_responses.ERR_UNKNOWNMODE
    message = f"{unknown_mode_num} {unknown_command} {unknown_mode_info}"
    user.send_string_to_client(message)


def error_not_enough_params(user: mantatail.UserConnection, command: str) -> None:
    (not_enough_params_num, not_enough_params_info) = irc_responses.ERR_NEEDMOREPARAMS
    message = f"{not_enough_params_num} {user.nick} {command} {not_enough_params_info}"
    user.send_string_to_client(message)
