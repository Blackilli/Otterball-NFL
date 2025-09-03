import datetime
import enum

import logging

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


class BetPollAnswer(enum.IntEnum):
    HOME = 0
    AWAY = 1
    TIE = 2

    def to_db_enum(self):
        if self == BetPollAnswer.HOME:
            return models.Outcome.HOME
        elif self == BetPollAnswer.AWAY:
            return models.Outcome.AWAY
        elif self == BetPollAnswer.TIE:
            return models.Outcome.TIE


class BetPoll(discord.Poll):
    def __init__(
        self,
        *args,
        home_team: str,
        home_team_emoji: discord.Emoji,
        away_team: str,
        away_team_emoji: discord.Emoji,
        tieable: bool = False,
        **kwargs,
    ):
        super().__init__(
            question=PollMedia(f"{home_team} - {away_team}"), *args, **kwargs
        )
        for answer in sorted(BetPollAnswer):
            if answer == BetPollAnswer.HOME:
                self.add_answer(text=home_team, emoji=home_team_emoji)
            elif answer == BetPollAnswer.AWAY:
                self.add_answer(text=away_team, emoji=away_team_emoji)
            elif answer == BetPollAnswer.TIE and tieable:
                self.add_answer(text="Tie", emoji="🤝")

    @property
    def answer_home(self):
        return self.answers[BetPollAnswer.HOME]

    @property
    def answer_away(self):
        return self.answers[BetPollAnswer.AWAY]

    @property
    def answer_tie(self):
        return self.answers[BetPollAnswer.TIE]


class MyClient(discord.Client):
    db: sqlalchemy.engine.Engine

    def __init__(self, db_engine: sqlalchemy.engine.Engine, *args, **kwargs):
        self.db = db_engine
        super().__init__(*args, **kwargs)

    async def setup_hook(self) -> None:
        self.check_polls.start()
        pass

    async def close_poll(self, db_poll: models.Poll):
        channel = await self.fetch_channel(db_poll.channel_id)
        message = await channel.fetch_message(db_poll.message_id)
        await message.poll.end()
        db_poll.closed = True

    @tasks.loop(seconds=10)
    async def check_polls(self):
        polls: list[models.Poll] = []
        with Session(self.db) as session:
            stmt = (
                select(models.Poll)
                .join(models.Game)
                .where(models.Poll.closed == False)
                .where(models.Game.kickoff <= datetime.datetime.now())
            )
            for poll in session.scalars(stmt).all():
                polls.append(poll)
        with Session(self.db) as session:
            for poll in polls:
                poll: models.Poll
                if poll.closed:
                    continue
                try:
                    await self.close_poll(poll)
                except Exception as e:
                    print(e)
                    continue
            session.commit()

    @tasks.loop(seconds=10)
    async def check_results_posted(self):
        polls: list[models.Poll] = []

        with Session(self.db) as session:
            stmt = (
                select(models.Poll)
                .join(models.Game)
                .where(models.Poll.result_posted == False)
                .where(models.Game.result != None)
            )
            for poll in session.scalars(stmt).all():
                polls.append(poll)
        for db_poll in polls:
            with Session(self.db) as session:
                poll: models.Poll | None = session.get(models.Poll, db_poll.id)
                if not poll:
                    raise Exception("Poll not found")
                channel = await self.get_or_fetch_channel(poll.channel_id)
                poll_msg = await channel.fetch_message(poll.message_id)
                result_txt = "The game has ended!\n"
                if poll.game.result == 0:
                    result_txt += "It's a tie, no one won!!! \n-# lol not gonna happen anyway. I want to thank my mom <3"
                else:
                    winner_emoji = self.fetch_application_emoji(
                        poll.game.winner.emoji_id
                    )
                    result_txt += f"The winner is {winner_emoji} {poll.game.winner.name} {winner_emoji}!\n"
                result_txt += "-# GG "
                for bet in poll.game.bets:
                    if bet.channel_id != poll.channel_id:
                        continue
                    if bet.choice != bet.game.outcome:
                        continue
                    user = await self.get_or_fetch_user(bet.user_id)
                    result_txt += f"{user.name}, "
                result_txt = result_txt[:-2]
                await poll_msg.reply(result_txt)
                poll.result_posted = True
                session.commit()

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

    @check_polls.before_loop
    async def before_check_polls(self):
        await self.wait_until_ready()

    async def post_leaderboards(self):
        channels: dict[int, dict[int, int]] = {}
        with Session(self.db) as session:
            stmt = select(models.Channel)
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

        print(f"{found_user=}")
        print(f"{db_polls=}")
        for db_poll in db_polls:
            with Session(self.db) as session:
                db_poll: models.Poll | None = session.get(models.Poll, db_poll.id)
                print(db_poll)
                if not db_poll:
                    raise Exception("Poll not found")
                channel = await self.fetch_channel(db_poll.channel_id)
                message = await channel.fetch_message(db_poll.message_id)
                poll = message.poll
                found_bets: dict[int, models.Bet] = {
                    bet.user_id: bet for bet in db_poll.game.bets
                }
                print(f"{found_bets=}")

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

    async def post_poll(self, game_id: str):
        with Session(self.db) as session:
            stmt = select(Game).where(Game.id == game_id)
            game: Game = session.scalars(stmt).first()
            if not game:
                raise Exception("Game not found")
            stmt = select(Channel)
            channels: set[Channel] = set(session.scalars(stmt).all())
            for poll in game.polls:
                channels.remove(poll.channel)
            home_emoji = await self.fetch_application_emoji(game.home_team.emoji_id)
            away_emoji = await self.fetch_application_emoji(game.away_team.emoji_id)

            for db_channel in channels:
                channel = await self.fetch_channel(db_channel.id)
                poll = discord.Poll(
                    PollMedia(f"{game.home_team.name} - {game.away_team.name}"),
                    duration=(game.kickoff - datetime.datetime.now()),
                )
                for answer in sorted(BetPollAnswer):
                    if answer == BetPollAnswer.HOME:
                        poll.add_answer(text=game.home_team.name, emoji=home_emoji)
                    elif answer == BetPollAnswer.AWAY:
                        poll.add_answer(text=game.away_team.name, emoji=away_emoji)
                    elif answer == BetPollAnswer.TIE and game.gametype_id == "REG":
                        poll.add_answer(text="Tie", emoji="🤝")
                try:
                    content = f"# {home_emoji} {game.home_team.name} - {game.away_team.name} {away_emoji}"
                    content += f"### 🏈   {game.gametype.name}"
                    content += f"### 📅   <t:{int(game.kickoff.timestamp())}:F> "
                    content += f"### ⏳   <t:{int(game.kickoff.timestamp())}:R>"
                    content += (
                        f"-# Polls may close early, so don't vote on the last second "
                    )

                    if db_channel.role_id:
                        content += (
                            await channel.guild.fetch_role(db_channel.role_id)
                        ).mention

                    msg = await channel.send(
                        content=content,
                        poll=poll,
                    )

                    session.add(
                        models.Poll(
                            message_id=msg.id,
                            channel_id=db_channel.id,
                            game_id=game.id,
                        )
                    )
                    session.commit()
                except HTTPException as e:
                    print(e)
                    continue

        return

    async def populate_team(self, team: pd.Series, emoji_id: int = 0):
        # print(team)
        # print(team.team_abbr, team.team_logo_wikipedia)
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
                    print(e)

    async def on_ready(self):
        print(f"Logged on as {self.user}!")
        await self.init_db()
        async for guild in self.fetch_guilds():
            roles = await guild.fetch_roles()
            for role in roles:
                print(f"{guild.name}: {role} ({role.id})")
            channels = await guild.fetch_channels()
            for channel in channels:
                print(f"{guild.name}: {channel} ({channel.id})")
        # await self.post_poll("2025_01_DAL_PHI")
        # await self.update_all_bets()
        print("LOL")

    async def on_message(self, message: discord.Message):
        logger.info(f"Message from {message.author}: {message.content}")
        logger.info(f"Channel: {message.channel.id}")


def main():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.messages = True

    engine = create_engine(settings.DB_CONNECTION_STRING, echo=True)
    Base.metadata.create_all(engine)

    client = MyClient(intents=intents, db_engine=engine)
    client.run(settings.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
