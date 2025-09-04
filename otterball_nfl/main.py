import datetime
import enum

import logging
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

    def __init__(self, db_engine: sqlalchemy.engine.Engine, *args, **kwargs):
        self.db = db_engine
        super().__init__(*args, **kwargs)

    async def setup_hook(self) -> None:
        self.close_polls.start()
        self.create_new_polls.start()
        self.post_results.start()

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

            channel = await self.fetch_channel(db_channel.id)
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
        with Session(self.db) as session:
            stmt = (
                select(models.Poll)
                .join(models.Game)
                .where(models.Poll.closed == False)
                .where(models.Game.kickoff <= datetime.datetime.now(ZoneInfo("UTC")))
            )
            for poll in session.scalars(stmt).all():
                try:
                    channel = await self.fetch_channel(poll.channel_id)
                    message = await channel.fetch_message(poll.message_id)
                    if message.poll.victor_answer is not None:
                        await message.poll.end()
                    poll.closed = True
                except Exception as e:
                    logger.error(e)
                    continue
            session.commit()

    @close_polls.before_loop
    async def before_check_polls(self):
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
                    result_txt = "The game has ended!\n"
                    if poll.game.outcome == models.Outcome.TIE:
                        result_txt += "It's a tie, no one won!!! \n-# lol not gonna happen anyway. I want to thank my mom <3"
                    else:
                        winner_emoji = self.fetch_application_emoji(
                            poll.game.winner.emoji_id
                        )
                        result_txt += f"The winner is {winner_emoji} {poll.game.winner.name} {winner_emoji}!\n"
                    result_txt += "-# GG "

                    stmt = (
                        select(models.Bet)
                        .where(models.Bet.channel_id == poll.channel_id)
                        .where(models.Bet.game_id == poll.game_id)
                        .where(models.Bet.choice == poll.game.outcome)
                    )
                    winner_bets: list[models.Bet] = list(session.scalars(stmt).all())
                    for bet in winner_bets:
                        user = await self.get_or_fetch_user(bet.user_id)
                        result_txt += f"{user.name}, "
                    if len(winner_bets) == 0:
                        result_txt += "nobody......... What is wrong with you guys?!"
                    else:
                        result_txt = result_txt[:-2]
                    await poll_msg.reply(result_txt)
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
            channel = await self.fetch_channel(channel_id)
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
            stmt = select(Team).where(Team.id == team.team_abbr)
            if session.scalars(stmt).first():
                return
            db_team = Team(
                id=team.team_abbr,
                name=team.team_name,
                logo=team.team_logo_wikipedia,
                emoji_id=emoji_id,
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
                    session.add(game_type)
                    session.commit()
                except Exception as e:
                    logger.error(e)

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

    async def on_message(self, message: discord.Message):
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
