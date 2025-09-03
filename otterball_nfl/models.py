from __future__ import annotations

import datetime
import enum

from sqlalchemy import (
    ForeignKey,
    DateTime,
    String,
    Boolean,
    Enum,
    BigInteger,
    Integer,
    and_,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, foreign


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channel"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    role_id: Mapped[int] = mapped_column(BigInteger)

    bets: Mapped[list[Bet]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
    )
    polls: Mapped[list[Poll]] = relationship(
        back_populates="channel",
    )

    gametype_scaling: Mapped[list[GameTypeScaling]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Channel(id={self.id}, name={self.name})>"


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    bets: Mapped[list[Bet]] = relationship(
        back_populates="user",
    )

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username})>"


# class GameType(enum.StrEnum):
#     REGULAR = "REG"
#     DIV = "DIV"
#     CONFERENCE = "CON"
#     SUPERBOWL = "SB"


class GameType(Base):
    __tablename__ = "gametype"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)

    games: Mapped[list[Game]] = relationship(
        back_populates="gametype",
        cascade="all, delete-orphan",
    )

    scaling: Mapped[list[GameTypeScaling]] = relationship(
        back_populates="gametype",
    )

    def __repr__(self):
        return f"<GameType(id={self.id}, name={self.name})>"

    def __str__(self):
        return self.name


class Outcome(enum.IntEnum):
    NOT_FINISHED = -1
    HOME = 0
    AWAY = 1
    TIE = 2

    @staticmethod
    def from_result(result: int | Mapped[int] | None):
        if result is None:
            return Outcome.NOT_FINISHED
        result = int(result)
        if result == 0:
            return Outcome.TIE
        elif result < 0:
            return Outcome.AWAY
        elif result > 0:
            return Outcome.HOME


class Game(Base):
    __tablename__ = "game"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    home_team_id: Mapped[str] = mapped_column(ForeignKey("team.id"))
    away_team_id: Mapped[str] = mapped_column(ForeignKey("team.id"))
    home_score: Mapped[int] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int] = mapped_column(Integer, nullable=True)
    result: Mapped[int] = mapped_column(Integer, nullable=True)
    outcome: Mapped[Outcome] = mapped_column(
        Enum(Outcome), default=Outcome.NOT_FINISHED
    )
    gametype_id: Mapped[str] = mapped_column(ForeignKey("gametype.id"))
    kickoff: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    home_team: Mapped[Team] = relationship(
        back_populates="home_games",
        foreign_keys=[home_team_id],
    )
    away_team: Mapped[Team] = relationship(
        back_populates="away_games",
        foreign_keys=[away_team_id],
    )
    bets: Mapped[list[Bet]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
    )
    polls: Mapped[list[Poll]] = relationship(
        back_populates="game",
    )
    gametype: Mapped[GameType] = relationship(
        back_populates="games",
    )

    @property
    def winner(self) -> Team | None:
        if self.outcome == Outcome.HOME:
            return self.home_team
        elif self.outcome == Outcome.AWAY:
            return self.away_team
        return None

    @property
    def winner_score(self) -> int | None:
        if self.outcome == Outcome.HOME:
            return self.home_score
        elif self.outcome == Outcome.AWAY:
            return self.away_score
        return None

    @property
    def loser(self) -> Team | None:
        if self.outcome == Outcome.HOME:
            return self.away_team
        elif self.outcome == Outcome.AWAY:
            return self.home_team
        return None

    @property
    def loser_score(self) -> int | None:
        if self.outcome == Outcome.HOME:
            return self.away_score
        elif self.outcome == Outcome.AWAY:
            return self.home_score
        return None


class Team(Base):
    __tablename__ = "team"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=False)
    logo: Mapped[str] = mapped_column(String)
    emoji_id: Mapped[int] = mapped_column(BigInteger)

    games: Mapped[list[Game]] = relationship(
        primaryjoin=or_(id == Game.home_team_id, id == Game.away_team_id),
        viewonly=True,
    )
    home_games: Mapped[list[Game]] = relationship(
        back_populates="home_team",
        cascade="all, delete-orphan",
        primaryjoin=and_(id == Game.home_team_id, id != Game.away_team_id),
    )
    away_games: Mapped[list[Game]] = relationship(
        back_populates="away_team",
        cascade="all, delete-orphan",
        primaryjoin=and_(id == Game.away_team_id, id != Game.home_team_id),
    )


class Poll(Base):
    __tablename__ = "poll"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"))
    message_id: Mapped[int] = mapped_column(BigInteger)
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"))
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    result_posted: Mapped[bool] = mapped_column(Boolean, default=False)

    channel: Mapped[Channel] = relationship(
        back_populates="polls",
    )
    game: Mapped[Game] = relationship(
        back_populates="polls",
    )


class GameTypeScaling(Base):
    __tablename__ = "gametype_scaling"

    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"), primary_key=True)
    gametype_id: Mapped[str] = mapped_column(
        ForeignKey("gametype.id"), primary_key=True
    )
    factor: Mapped[int] = mapped_column(Integer)

    channel: Mapped[Channel] = relationship(
        back_populates="gametype_scaling",
    )
    gametype: Mapped[GameType] = relationship(
        back_populates="scaling",
    )


class Bet(Base):
    __tablename__ = "bet"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    game_id: Mapped[str] = mapped_column(ForeignKey("game.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"))
    choice: Mapped[Outcome] = mapped_column(Enum(Outcome))

    possible_points = relationship(
        GameTypeScaling,
        primaryjoin=and_(
            channel_id == foreign(GameTypeScaling.channel_id),
            foreign(GameTypeScaling.gametype_id) == Game.gametype_id,
        ),
        viewonly=True,
        uselist=False,
    )

    user: Mapped[User] = relationship(
        back_populates="bets",
    )
    game: Mapped[Game] = relationship(
        back_populates="bets",
    )
    channel: Mapped[Channel] = relationship(
        back_populates="bets",
    )

    @property
    def earned_points(self):
        if self.choice == self.game.outcome:
            pass
        elif self.possible_points:
            return self.possible_points.factor
        return 0
