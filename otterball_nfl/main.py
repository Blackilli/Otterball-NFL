import asyncio
import datetime
import enum

import logging
import traceback
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

import discord
import nfl_data_py as nfl
import httpx
import sqlalchemy
import pandas as pd
from discord import HTTPException
from discord.ext import tasks
from discord.poll import PollMedia
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
import settings
from models import Base, Team, Game, GameType, Channel
from sqlalchemy import create_engine

logger = logging.getLogger("mybot")


class MyClient(discord.Client):
    db: sqlalchemy.engine.Engine

    delete_result_msg_for: set[int] = set()

    def __init__(self, db_engine: sqlalchemy.engine.Engine, *args, **kwargs):
        self.db = db_engine
        super().__init__(*args, **kwargs)

    async def setup_hook(self) -> None:
        with Session(self.db) as session:
            stmt = select(models.Channel).where(
                models.Channel.delete_result_msg == True
            )
            for channel in session.scalars(stmt).all():
                self.delete_result_msg_for.add(channel.id)
        self.close_polls.start()
        self.create_new_polls.start()
        self.post_results.start()
        self.sync_bets.start()

    async def sync_poll_bets(self, db_poll_id):
        with Session(self.db) as session:
            db_poll: models.Poll | None = session.get(models.Poll, db_poll_id)
            if not db_poll:
                raise Exception("Poll not found")
            channel = await self.get_or_fetch_channel(db_poll.channel_id)
            message = await channel.fetch_message(db_poll.message_id)
            poll = message.poll
            voter_ids: set[int] = set()
            for answer in poll.answers:
                async for voter in answer.voters():
                    voter_ids.add(voter.id)
                    db_user = session.get(models.User, voter.id)
                    if not db_user:
                        db_user = models.User(id=voter.id, username=voter.name)
                        session.add(db_user)
                    stmt = (
                        select(models.Bet)
                        .where(models.Bet.user_id == voter.id)
                        .where(models.Bet.game_id == db_poll.game_id)
                        .where(models.Bet.channel_id == db_poll.channel_id)
                    )
                    db_bet = session.scalars(stmt).first()
                    if db_bet:
                        db_bet.choice = answer.id - 1
                    else:
                        db_bet = models.Bet(
                            user_id=voter.id,
                            game_id=db_poll.game_id,
                            channel_id=db_poll.channel_id,
                            choice=answer.id - 1,
                        )
                        session.add(db_bet)

            stmt = (
                select(models.Bet)
                .where(models.Bet.game_id == db_poll.game_id)
                .where(models.Bet.channel_id == db_poll.channel_id)
                .where(models.Bet.user_id.notin_(voter_ids))
            )
            for deleted_bet in session.scalars(stmt).all():
                session.delete(deleted_bet)

            session.commit()

    @tasks.loop(minutes=5)
    async def sync_bets(self):
        db_polls: list[models.Poll] = []
        with Session(self.db) as session:
            stmt = select(models.Poll).where(models.Poll.closed == False)
            for db_poll in session.scalars(stmt).all():
                db_polls.append(db_poll)
        for db_poll in db_polls:
            try:
                await self.sync_poll_bets(db_poll.id)
            except Exception as e:
                logger.error(e)
                continue

    @sync_bets.before_loop
    async def before_sync_bets(self):
        await self.wait_until_ready()

    async def post_poll(self, poll_id: int):
        with Session(self.db) as session:
            db_poll: models.Poll | None = session.get(models.Poll, poll_id)
            db_channel: models.Channel | None = session.get(
                models.Channel, db_poll.channel_id
            )
            db_game: models.Game | None = session.get(models.Game, db_poll.game_id)
            home_team: models.Team | None = session.get(
                models.Team, db_game.home_team_id
            )
            away_team: models.Team | None = session.get(
                models.Team, db_game.away_team_id
            )
            home_emoji = await self.fetch_application_emoji(home_team.emoji_id)
            away_emoji = await self.fetch_application_emoji(away_team.emoji_id)

            channel = await self.get_or_fetch_channel(db_channel.id)
            poll = discord.Poll(
                PollMedia(f"{home_team.name} - {away_team.name}"),
                duration=(
                    db_game.kickoff - datetime.datetime.now(datetime.timezone.utc)
                ),
            )
            for answer in sorted(models.Outcome):
                if answer == models.Outcome.HOME:
                    poll.add_answer(text=home_team.name, emoji=home_emoji)
                elif answer == models.Outcome.AWAY:
                    poll.add_answer(text=away_team.name, emoji=away_emoji)
                elif answer == models.Outcome.TIE and db_game.gametype_id == "REG":
                    poll.add_answer(text="Tie", emoji="🤝")
            try:
                content = (
                    f"# {home_emoji} {home_team.name} - {away_team.name} {away_emoji}"
                )
                content += f"\n### 🏈   {db_game.gametype.name}"
                db_scaling = session.get(
                    models.GameTypeScaling, (db_channel.id, db_game.gametype_id)
                )
                if db_scaling and db_scaling.factor:
                    content += f" (Grants you {db_scaling.factor} point{'' if db_scaling.factor == 1 else 's'})"
                content += f"\n### 📅   <t:{int(db_game.kickoff.timestamp())}:F> "
                content += f"\n### ⏳   <t:{int(db_game.kickoff.timestamp())}:R>"
                content += (
                    f"\n-# Polls may close early, so don't vote on the last second "
                )

                if db_channel.role_id:
                    content += (
                        await channel.guild.fetch_role(db_channel.role_id)
                    ).mention

                msg = await channel.send(
                    content=content,
                    poll=poll,
                )
                db_poll.message_id = msg.id
                session.add(db_poll)
                session.commit()
                await msg.pin()
            except HTTPException as e:
                logger.error(e)

    @tasks.loop(seconds=10)
    async def create_new_polls(self):
        new_polls: list[models.Poll] = []
        with Session(self.db) as session:
            stmt = (
                select(models.Poll)
                .join(models.Channel)
                .where(models.Channel.active == True)
                .where(models.Poll.message_id == None)
            )
            for db_poll in session.scalars(stmt).all():
                new_polls.append(db_poll)
        for db_poll in new_polls:
            await self.post_poll(db_poll.id)

    @create_new_polls.before_loop
    async def before_create_new_polls(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def close_polls(self):
        poll_ids: list[int] = []
        with Session(self.db) as session:
            stmt = (
                select(models.Poll)
                .join(models.Game)
                .where(models.Poll.closed == False)
                .where(models.Game.kickoff <= datetime.datetime.now(ZoneInfo("UTC")))
            )
            for poll in session.scalars(stmt).all():
                poll_ids.append(poll.id)
                try:
                    channel = await self.get_or_fetch_channel(poll.channel_id)
                    message = await channel.fetch_message(poll.message_id)
                    if not message.poll.is_finalised():
                        await message.poll.end()
                    poll.closed = True
                    if message.pinned:
                        await message.unpin()
                except Exception as e:
                    logger.error(f"Poll {poll.id}: {e}")
                    continue
            session.commit()
        for poll_id in poll_ids:
            await self.sync_poll_bets(poll_id)

    @close_polls.before_loop
    async def before_close_polls(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def post_results(self):
        polls: list[models.Poll] = []

        with Session(self.db) as session:
            stmt = (
                select(models.Poll)
                .join(models.Game)
                .join(models.Channel)
                .where(models.Channel.active == True)
                .where(models.Poll.result_posted == False)
                .where(models.Game.result != None)
            )
            for poll in session.scalars(stmt).all():
                polls.append(poll)

        for db_poll in polls:
            try:
                with Session(self.db) as session:
                    poll: models.Poll | None = session.get(models.Poll, db_poll.id)
                    if not poll:
                        raise Exception("Poll not found")
                    channel = await self.get_or_fetch_channel(poll.channel_id)
                    poll_msg = await channel.fetch_message(poll.message_id)
                    db_game: models.Game | None = session.get(
                        models.Game, db_poll.game_id
                    )
                    scaling: models.GameTypeScaling | None = session.get(
                        models.GameTypeScaling,
                        (db_poll.channel_id, db_game.gametype_id),
                    )
                    home_team_emoji = await self.fetch_application_emoji(
                        db_game.home_team.emoji_id
                    )
                    awayteam_emoji = await self.fetch_application_emoji(
                        db_game.away_team.emoji_id
                    )

                    embed = discord.Embed(
                        title=f"**Final Score**",
                        description=f"{db_game.gametype.name} ({scaling.factor} Otter Point{'' if scaling.factor == 1 else 's'})",
                        color=(
                            discord.Colour.from_str(db_game.winner.color)
                            if db_game.outcome != models.Outcome.TIE
                            and db_game.winner.color
                            else discord.Colour.blue()
                        ),
                    )
                    embed.add_field(
                        name=f"{home_team_emoji} {db_game.home_team.name}",
                        value=db_game.home_score,
                        inline=True,
                    )
                    embed.add_field(
                        name=f"{awayteam_emoji} {db_game.away_team.name}",
                        value=db_game.away_score,
                        inline=True,
                    )
                    embed.set_thumbnail(
                        url=(
                            db_game.winner.logo
                            if db_game.winner
                            else "https://static.wikia.nocookie.net/memepediadankmemes/images/c/cc/Wat8.jpg"
                        )
                    )

                    stmt = (
                        select(models.Bet)
                        .where(models.Bet.game_id == db_poll.game_id)
                        .where(models.Bet.choice == db_game.outcome)
                        .where(models.Bet.channel_id == db_poll.channel_id)
                    )
                    footer_text = ""
                    for db_bet in session.scalars(stmt).all():
                        try:
                            user = await self.get_or_fetch_user(db_bet.user_id)
                            footer_text += f"{user.mention}, "
                        except Exception as e:
                            logger.error(e)
                            footer_text += f"{db_bet.user.username}, "
                            continue
                    if len(footer_text) == 0:
                        footer_text += "nobody......... What is wrong with you guys?!"
                    else:
                        footer_text = "GG " + footer_text[:-2]
                    embed.add_field(
                        name="---------",
                        value=footer_text,
                        inline=False,
                    )
                    await poll_msg.reply(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                    poll.result_posted = True
                    session.commit()
            except Exception as e:
                logger.error(e)
                continue

    @post_results.before_loop
    async def before_post_results(self):
        await self.wait_until_ready()

    async def get_or_fetch_user(self, user_id: int):
        user = self.get_user(user_id)
        if user is None:
            user = await self.fetch_user(user_id)
        return user

    async def get_or_fetch_channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    async def post_leaderboards(self):
        channels: dict[int, dict[int, int]] = {}
        with Session(self.db) as session:
            stmt = select(models.Channel).where(models.Channel.active == True)
            for channel in session.scalars(stmt).all():
                leaderboard: dict[int, int] = channels[channel.id]
                for bet in channel.bets:
                    leaderboard[bet.user_id] = (
                        leaderboard.get(bet.user_id, 0) + bet.earned_points
                    )
                channels[channel.id] = leaderboard
        for channel_id, leaderboard in channels.items():
            channel = await self.get_or_fetch_channel(channel_id)
            leaderboard_str = "# Leaderboard:\n```"
            for idx, (user_id, points) in enumerate(
                sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
            ):
                user = await self.fetch_user(user_id)
                leaderboard_str += f"{idx + 1}. {user.name}: {points}\n"
            leaderboard_str += "```"
            await channel.send(leaderboard_str)

        pass

    async def update_all_bets(self):
        found_user: set[int] = set()
        db_polls: list[models.Poll] = []
        with Session(self.db) as session:
            stmt = select(models.User)
            for user in session.scalars(stmt).all():
                found_user.add(user.id)
            stmt = select(models.Poll).where(models.Poll.closed == False)
            for db_poll in session.scalars(stmt).all():
                db_polls.append(db_poll)

        logger.debug(f"{found_user=}")
        logger.debug(f"{db_polls=}")
        for db_poll in db_polls:
            with Session(self.db) as session:
                db_poll: models.Poll | None = session.get(models.Poll, db_poll.id)
                logger.debug(db_poll)
                if not db_poll:
                    raise Exception("Poll not found")
                channel = await self.fetch_channel(db_poll.channel_id)
                message = await channel.fetch_message(db_poll.message_id)
                poll = message.poll
                found_bets: dict[int, models.Bet] = {
                    bet.user_id: bet for bet in db_poll.game.bets
                }
                logger.debug(f"{found_bets=}")

                for answer in poll.answers:
                    voters = [voter async for voter in answer.voters()]
                    for voter in voters:
                        if voter.id in found_bets.keys():
                            bet = found_bets.pop(voter.id)
                            bet.choice = answer.id - 1
                        else:
                            if voter.id not in found_user:
                                user = await self.fetch_user(voter.id)
                                db_user = models.User(id=user.id, username=user.name)
                                session.add(db_user)
                                found_user.add(user.id)
                            db_bet = models.Bet(
                                user_id=voter.id,
                                game_id=db_poll.game_id,
                                channel_id=db_poll.channel_id,
                                choice=answer.id - 1,
                            )
                            session.add(db_bet)
                for bet in found_bets.values():
                    session.delete(bet)
                if poll.victor_answer:
                    db_poll.closed = True
                session.commit()

    async def init_db(self):
        await self.populate_game_types()
        await self.populate_game_type_scaling()
        await self.populate_all_teams()

    async def populate_team(self, team: pd.Series, emoji_id: int = 0):
        if emoji_id == 0:
            response = httpx.get(team.team_logo_wikipedia, follow_redirects=True)
            image = response.content
            emoji = await self.create_application_emoji(
                name=team.team_abbr, image=image
            )
            emoji_id = emoji.id
        with Session(self.db) as session:
            db_team: models.Team | None = session.get(models.Team, team.team_abbr)
            if db_team:
                db_team.name = team.team_name
                db_team.logo = team.team_logo_wikipedia
                db_team.emoji_id = emoji_id
                db_team.color = team.team_color
            else:
                db_team = Team(
                    id=team.team_abbr,
                    name=team.team_name,
                    logo=team.team_logo_wikipedia,
                    emoji_id=emoji_id,
                    color=team.team_color,
                )
                session.add(db_team)
            session.commit()

    async def populate_all_teams(self):
        emojis = await self.fetch_application_emojis()

        for team in nfl.import_team_desc().iloc:
            emoji_id = 0
            for emoji in emojis:
                if emoji.name == team.team_abbr:
                    emoji_id = emoji.id
            await self.populate_team(team, emoji_id)

    async def populate_game_types(self):
        game_types = [
            GameType(
                id="REG",
                name="Regular Season",
            ),
            GameType(
                id="DIV",
                name="Divisional Round",
            ),
            GameType(
                id="WC",
                name="Wild Card Round",
            ),
            GameType(
                id="CON",
                name="Conference Championship",
            ),
            GameType(
                id="SB",
                name="Super Bowl",
            ),
        ]
        with Session(self.db) as session:
            for game_type in game_types:
                try:
                    db_game_type: models.GameType | None = session.get(
                        models.GameType, game_type.id
                    )
                    if db_game_type:
                        return
                    session.add(game_type)
                    session.commit()
                except Exception as e:
                    logger.error(e)

    async def populate_game_type_scaling(self):
        with Session(self.db) as session:
            stmt = select(models.GameType)
            game_types = session.scalars(stmt).all()
            stmt = select(models.Channel)
            channels = session.scalars(stmt).all()
            for game_type in game_types:
                for channel in channels:
                    game_type_scaling: models.GameTypeScaling | None = session.get(
                        models.GameTypeScaling, (channel.id, game_type.id)
                    )
                    if game_type_scaling:
                        continue
                    game_type_scaling = models.GameTypeScaling(
                        channel_id=channel.id,
                        gametype_id=game_type.id,
                        factor=1,
                    )
                    session.add(game_type_scaling)
            session.commit()

    async def on_ready(self):
        print(f"Logged on as {self.user}!")
        await self.init_db()
        async for guild in self.fetch_guilds():
            roles = await guild.fetch_roles()
            for role in roles:
                print(f"{guild.name}: {role} ({role.id}) {role.members}#")
            channels = await guild.fetch_channels()
            for channel in channels:
                print(f"{guild.name}: {channel} ({channel.id})")
        # await self.update_all_bets()
        print("LOL")

    async def delete_message_by_link(self, message_link: str):
        channel_id, message_id = message_link.split("/")[-2:]
        channel = await self.get_or_fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        await message.delete()

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            match message.type:
                case discord.MessageType.default:
                    pass
                case discord.MessageType.poll_result:
                    if message.id in self.delete_result_msg_for:
                        await message.delete()
        logger.debug(f"Message from {message.author}: {message.content}")
        logger.debug(f"Channel: {message.channel.id}")


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.messages = True

    engine = create_engine(settings.DB_CONNECTION_STRING, echo=False)
    Base.metadata.create_all(engine)

    client = MyClient(intents=intents, db_engine=engine)
    client.run(settings.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
