import sqlalchemy as sa


def check_enum_exists(op, enum_name: str) -> bool:
    connection = op.get_bind()
    result = connection.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{enum_name}')")
    )
    return result.scalar()


def create_enum_if_not_exists(op, enum_name: str, enum_values: list[str]) -> None:
    if not check_enum_exists(op, enum_name):
        enum_values_str = "', '".join(enum_values)
        op.execute(f"CREATE TYPE {enum_name} AS ENUM ('{enum_values_str}')")


def drop_enum_if_exists(op, enum_name: str) -> None:
    if check_enum_exists(op, enum_name):
        op.execute(f"DROP TYPE {enum_name} CASCADE")
