import datetime
from zoneinfo import ZoneInfo

import nfl_data_py as nfl
import pandas as pd
import httpx
from celery import Celery, Task, signals
from celery.utils.log import get_task_logger
from numpy import isnan
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from otterball_nfl import settings, models
from otterball_nfl.models import Game

logger = get_task_logger(__name__)
logger.setLevel(settings.LOG_LEVEL)

engine = create_engine(settings.DB_CONNECTION_STRING, echo=False)

app = Celery("tasks", broker=settings.CELERY_BROKER_URL)
app.config_from_object("otterball_nfl.celeryconfig")


@app.task(bind=True, ignore_result=True)
def update_games(self: Task, season: int):
    games = nfl.import_schedules([season])
    games["datetime_str"] = games["gameday"] + " " + games["gametime"]
    games["kickoff"] = pd.to_datetime(games["datetime_str"], format="%Y-%m-%d %H:%M")
    games["kickoff"] = games["kickoff"].dt.tz_localize(ZoneInfo("America/New_York"))
    games["kickoff_utc"] = games["kickoff"].dt.tz_convert(ZoneInfo("UTC"))

    with Session(engine) as session:
        for game in games.iloc:
            try:
                db_game = session.get(Game, game.game_id)
                if db_game:
                    db_game.home_score = (
                        int(game.home_score) if not isnan(game.home_score) else None
                    )
                    db_game.away_score = (
                        int(game.away_score) if not isnan(game.away_score) else None
                    )
                    db_game.kickoff = game.kickoff_utc
                    db_game.result = (
                        int(game.result) if not isnan(game.result) else None
                    )
                    db_game.outcome = models.Outcome.from_result(db_game.result)
                else:
                    db_game = Game(
                        id=game.game_id,
                        gametype_id=game.game_type,
                        home_team_id=game.home_team,
                        away_team_id=game.away_team,
                        home_score=(
                            int(game.home_score) if not isnan(game.home_score) else None
                        ),
                        away_score=(
                            int(game.away_score) if not isnan(game.away_score) else None
                        ),
                        kickoff=game.kickoff_utc,
                        result=int(game.result) if not isnan(game.result) else None,
                    )
                    db_game.outcome = models.Outcome.from_result(db_game.result)
                    session.add(db_game)
            except Exception as e:
                print(e)
                continue
            if game.kickoff_utc < pd.Timestamp.now(ZoneInfo("UTC")):
                stmt = (
                    select(models.Poll)
                    .where(models.Poll.game_id == game.game_id)
                    .where(models.Poll.closed == False)
                )
                for poll in session.scalars(stmt).all():
                    pass
        session.commit()


@app.task(bind=True, ignore_result=True)
def create_polls(self: Task):
    logger.info("Creating polls")
    with Session(engine) as session:
        stmt = select(models.Channel).where(models.Channel.active == True)
        channels: set[models.Channel] = set(session.scalars(stmt).all())
        stmt = select(models.Game).where(
            models.Game.kickoff.between(
                datetime.datetime.now(ZoneInfo("UTC")),
                datetime.datetime.now(ZoneInfo("UTC")) + datetime.timedelta(days=7),
            )
        )
        for game in session.scalars(stmt).all():
            print(game.id)
            for channel in channels:
                print(channel.id)
                stmt = (
                    select(models.Poll)
                    .where(models.Poll.game_id == game.id)
                    .where(models.Poll.channel_id == channel.id)
                )
                if session.scalars(stmt).first():
                    logger.info(f"Poll already exists for {game.id} in {channel.id}")
                    continue
                session.add(
                    models.Poll(
                        channel=channel,
                        game=game,
                        closed=False,
                        result_posted=False,
                    )
                )
        session.commit()


@signals.worker_ready.connect
def update_espn_teams(*args, **kwargs):
    with httpx.Client() as client:
        response = client.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
        )
        teams = response.json()["sports"][0]["leagues"][0]["teams"]
        with Session(engine) as session:
            for team in teams:
                team = team["team"]
                db_team = session.get(models.Team, str(team["abbreviation"]).upper())
                if not db_team:
                    logger.error(f"Team {team['abbreviation']} not found")
                    continue
                stmt = (
                    select(models.TeamIdentifier)
                    .where(models.TeamIdentifier.external_id == str(team["id"]))
                    .where(models.TeamIdentifier.source == models.ApiSource.ESPN_V2)
                )
                if session.scalars(stmt).first() is None:
                    session.add(
                        models.TeamIdentifier(
                            team_id=db_team.id,
                            external_id=str(team["id"]),
                            source=models.ApiSource.ESPN_V2,
                        )
                    )
            session.commit()
