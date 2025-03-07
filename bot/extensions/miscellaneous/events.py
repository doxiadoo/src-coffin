
from discord.ext.commands import Cog
from discord import (
    Client,
    File,
    User,
    Member,
    Message,
    Reaction,
    Guild
)
import discord
from aiohttp import ClientSession, ClientPayloadError
from typing import Union, Optional, List
from asyncio import gather, sleep, ensure_future, Lock
from datetime import datetime, timedelta
from collections import defaultdict
from io import BytesIO
from aiomisc.backoff import asyncretry
from discord.http import handle_message_parameters, Route, iteration
from system.classes.database import Record
from system.services.media.avatar import get_hash
from system.classes.builtins import get_error
from loguru import logger
from humanize import naturaldelta
from system.patch.context import Context


class MiscellaneousEvents(Cog):
    def __init__(self, bot: Client):
        self.bot = bot
        self.to_send = []
        self.locks = defaultdict(Lock)
        self.avatar_history = {
            "guild_id": 1296957636243226716,
            "channel_id": 1305230241186316391,
            "worker_ids": [],
            "worker_tokens": iteration(["OTEyMjY4MzU4NDE2NzU2ODE2.GxPVhK.OxJ05Kt7FZweD63iMauWHDxjCzFpqkuqugNRmg", "OTcxMTc4NTgzNDcxMDM0NDM5.GyMA8j.d8ItR_aT50QJVp2-ITrfKO4JYtW2aPToFceXGA", "NzAwNTMxNzg2OTk1OTkwNTg5.GlE6fs.O-wEk6FFxbu5D70jtb_h_zKREmG79h-ulpPDb0", "MTA1NzI4MjU0MjA3MzU1NzA3Mg.Ganlz5.ntz2uR2D-FjAxTOphRc3NJd6ZSiRp6J2w2_qi0", "NzU5ODQ1NTg5MzQ5MDQwMTU5.GVh4Qa.DR4qsvtWuzxTzpQKkh2yNlB0oRrRASaWHz-HvM"])
        }
        self.channel_ids = [1305230241186316391]

    async def check_workers(self) -> Optional[List[str]]:
        invalid = []
        for token in self.avatar_history["worker_tokens"].data:
            async with ClientSession() as session:
                async with session.get("https://discord.com/api/v10/users/@me", headers = {"Authorization": f"Bot {token}"}) as response:
                    if response.status != 200:
                        logger.info(f"the token {token} is INVALID")
                        invalid.append(token)
        return invalid
    


    async def namehistory_event(self, before: Union[Member, User], after: Union[Member, User]):
        if before.name != after.name:
            name = before.name
            nt = "username"
        elif before.global_name != after.global_name:
            name = before.global_name
            nt = "globalname"
        elif before.display_name != after.display_name:
            name = before.display_name
            nt = "display"
        else:
            if not isinstance(before, Member):
                return
            else:
                try:
                    b_n = str(before.nick) or ""
                except Exception:
                    b_n = ""
                try:
                    a_n = str(after.nick) or ""
                except Exception:
                    a_n = ""
                if b_n != a_n:
                    if b_n == "":
                        return
                    name = b_n
                    nt = "nickname"
                else:
                    return
        if name is None:
            return
        await self.bot.db.execute(
            """INSERT INTO names (user_id, type, username, ts) VALUES($1, $2, $3, $4) ON CONFLICT(user_id, username, type, ts) DO NOTHING""",
            before.id,
            nt,
            name,
            datetime.now(),
        )

    @Cog.listener("on_username_change")
    async def tracked_username_change(self, before: Union[Member, User], after: Union[Member, User]):
        return await self.namehistory_event(before, after)
    
    @Cog.listener("on_guild_name_update")
    async def tracked_guild_name_change(self, guild: Guild):
        await self.bot.db.execute("""INSERT INTO guild_names (guild_id, name, ts) VALUES($1, $2, $3) ON CONFLICT(guild_id, name, ts) DO NOTHING""", guild.id, guild.name, datetime.now())

    async def avatar_to_file(self, user: User, url: str) -> str:
        return f"{user.id}.{url.split('.')[-1].split('?')[0]}"
    
    async def redistribute(self, url: str) -> str:
        async with ClientSession() as session:
            url = url.replace("discord.com", "coffin.bot").replace("discordapp.com", "coffin.bot")
            async with session.get(url) as response:
                if response.status == 200:
                    pass
        return url

    #@Cog.listener("on_message")
    async def on_avatarhistory_post(self, message: Message):
        if message.guild.id != self.avatar_history["guild_id"]:
            return
        if message.channel.id != self.avatar_history["channel_id"]:
            return
        if message.author.id not in self.avatar_history["worker_ids"]:
            return
        for i, attachment in enumerate(message.attachments, start = 0):
            user_id = attachment.filename.split(".")[0]
            avatar_hash = await get_hash(await attachment.read())
            await self.bot.db.execute("""INSERT INTO avatars (user_id, message_id, index, ts, avatar_hash, url) VALUES($1, $2, $3, $4, $5, $6)""", user_id, message.id, i, datetime.now(), avatar_hash, await self.redistribute(attachment.url))
            

    async def refetch(self, row: Record):
        token = next(self.avatar_history["worker_tokens"])
        message = await self.bot.http.request(
            route=Route(
                "GET",
                "/channels/{channel_id}/messages/{message_id}",
                channel_id=self.avatar_history["channel_id"],
                message_id=row.message_id,
            ),
            token=f"Bot {token}",
        )
        for i, attachment in enumerate(message["attachments"], start = 0):
            asset = await self.redistribute(attachment["url"])
            if i == row.index:
                row.url = asset
                row["url"] = asset
            try:
                await self.bot.db.execute("""UPDATE avatars SET url = $1 WHERE message_id = $2 AND index = $3""", asset, row.message_id, i)
            except Exception:
                pass
        return row

    @asyncretry(max_tries = 10, pause = 0.5)
    async def check_avatar(self, row: Record):
        data = None
        async with ClientSession() as session:
            async with session.get(row.url) as response:
                if response.status == 200:
                    data = row
        if not data:
            return await self.refetch(row)
        return data
    
    async def get_user_avatars(self, user: Union[User, Member]):
        rows = await self.bot.db.fetch("""SELECT * FROM avatars WHERE user_id = $1 ORDER BY ts DESC""", user.id)
        avatars = []
        for row in rows:
            avatars.append(await self.check_avatar(row))
        return avatars

                

    async def post_avatar(self, before: Union[Member, User], after: Union[Member, User]):
        async with self.locks["avatar_post"]:
            
            if not (channel := self.bot.get_channel(self.avatar_history["channel_id"])):
                return
            token = next(self.avatar_history["worker_tokens"])
#                channel = self.get_channel(channel_id)
            if after.display_avatar.url != after.default_avatar.url:
                try:
                    read_avatar = await after.display_avatar.read()
                    file = [
                        read_avatar,
                        await self.avatar_to_file(after, after.display_avatar.url),
                    ]
                    avatar_hash = await get_hash(read_avatar)
                except discord.errors.NotFound:
                    return
                if await self.bot.db.fetchrow("""SELECT * FROM avatars WHERE user_id = $1 AND avatar_hash = $2""", before.id, file[-1]):
                    return
                self.to_send.append(
                    {
                        "uid": after.id,
                        "un": after.name,
                        "file": file,
                        "ts": datetime.now(),
                        "hash": avatar_hash
                    }
                )

                if len(self.to_send) == 10:
                    files = [
                        File(
                            fp=BytesIO(a["file"][0]), filename=a["file"][1]
                        )
                        for a in self.to_send
                    ]
                    params = handle_message_parameters(  # noqa: F841
                        files = files
                    )
                    try:
                        async def send_it(**kwargs):
                            c = None
                            await sleep(3)
                            for i in range(5):
                                try:
                                    c = await channel.send(**kwargs)
                                    break
                                except ClientPayloadError:
                                    continue
                            return c
                        confirmed_message = await send_it(files = files, token=f"Bot {token}")
                        for i, attachment in enumerate(confirmed_message.attachments, start = 0):
                            user_id = self.to_send[i]["uid"]
                            ts = self.to_send[i]["ts"]
                            await self.bot.db.execute("""INSERT INTO avatars (user_id, message_id, index, ts, avatar_hash, url) VALUES($1, $2, $3, $4, $5, $6)""", user_id, confirmed_message.id, i, ts, self.to_send[i]["hash"], await self.redistribute(attachment.url))
                    except Exception as e:
                        logger.info(get_error(e))
                    self.to_send.clear()


    @Cog.listener("on_avatar_change")
    async def on_user_update(self, before: Union[Member, User], after: Union[Member, User]) -> None:
        if before.display_avatar != after.display_avatar:
            return await self.post_avatar(before, after)

    @Cog.listener("on_afk_check")
    async def on_afk_check(self, ctx: Context) -> None:
        try:
            return await self.do_afk_check(ctx)
        except Exception as e:
            return await self.bot.errors.handle_exceptions(ctx, e)

    async def do_afk_check(self, ctx: Context):
        message = ctx.message
        if ctx.command:
            return
        elif author_afk_since := await self.bot.db.fetchval(
            """
            DELETE FROM afk
            WHERE user_id = $1
            RETURNING date
            """,
            message.author.id, cached = False
        ):
            await ctx.normal(
                f"Welcome back, you were away for **{naturaldelta(timedelta(seconds=(datetime.now(tz=author_afk_since.tzinfo) - author_afk_since).seconds))}**",
                emoji="ðŸ‘‹",
                reference=message,
            )

        elif len(message.mentions) == 1 and (user := message.mentions[0]):
            if user_afk := await self.bot.db.fetchrow(
                """
                SELECT status, date FROM afk
                WHERE user_id = $1
                """,
                user.id, cached = False
            ):
                await ctx.normal(
                    f"{user.mention} is AFK: **{user_afk.status}** - {naturaldelta(timedelta(seconds=(datetime.now(tz=user_afk.date.tzinfo) - user_afk.date).seconds))}",
                    emoji="ðŸ’¤",
                    reference=message,
                )

    @Cog.listener("on_message_delete")
    async def on_new_snipe(self, message: Message):
        if message.author.id != self.bot.user.id:
            return await self.bot.snipes.add_entry("snipe", message)

    @Cog.listener("on_message_edit")
    async def on_new_editsnipe(self, before: Message, after: Message):
        if before.content != after.content and before.author.id != self.bot.user.id:
            return await self.bot.snipes.add_entry("editsnipe", before)


    @Cog.listener("on_reaction_remove")
    async def on_new_reactionsnipe(
        self, reaction: Reaction, user: Union[Member, User]
    ):
        return await self.bot.snipes.add_entry("rs", (user, reaction))                

async def setup(bot: Client):
    await bot.add_cog(MiscellaneousEvents(bot))
