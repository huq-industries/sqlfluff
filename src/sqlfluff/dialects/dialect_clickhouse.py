"""The clickhouse dialect.

https://clickhouse.com/
"""
from sqlfluff.core.dialects import load_raw_dialect
from sqlfluff.core.parser import (
    AnyNumberOf,
    AnySetOf,
    BaseSegment,
    Bracketed,
    Conditional,
    Dedent,
    Delimited,
    Indent,
    Matchable,
    OneOf,
    OptionallyBracketed,
    Ref,
    Sequence,
    SymbolSegment,
    TypedParser,
    StringLexer,
)
from sqlfluff.dialects import dialect_ansi as ansi
from sqlfluff.dialects.dialect_clickhouse_keywords import (
    UNRESERVED_KEYWORDS,
)

ansi_dialect = load_raw_dialect("ansi")

clickhouse_dialect = ansi_dialect.copy_as("clickhouse")
clickhouse_dialect.sets("unreserved_keywords").update(UNRESERVED_KEYWORDS)

clickhouse_dialect.insert_lexer_matchers(
    # https://clickhouse.com/docs/en/sql-reference/functions#higher-order-functions---operator-and-lambdaparams-expr-function
    [StringLexer("lambda", r"->", SymbolSegment, segment_kwargs={"type": "lambda"})],
    before="newline",
)

clickhouse_dialect.add(
    JoinTypeKeywords=OneOf(
        # This case INNER [ANY,ALL] JOIN
        Sequence("INNER", OneOf("ALL", "ANY", optional=True)),
        # This case [ANY,ALL] INNER JOIN
        Sequence(OneOf("ALL", "ANY", optional=True), "INNER"),
        # This case FULL ALL OUTER JOIN
        Sequence(
            "FULL",
            Ref.keyword("ALL", optional=True),
            Ref.keyword("OUTER", optional=True),
        ),
        # This case ALL FULL OUTER JOIN
        Sequence(
            Ref.keyword("ALL", optional=True),
            "FULL",
            Ref.keyword("OUTER", optional=True),
        ),
        # This case LEFT [OUTER,ANTI,SEMI,ANY,ASOF] JOIN
        Sequence(
            "LEFT",
            OneOf(
                "ANTI",
                "SEMI",
                OneOf("ANY", "ALL", optional=True),
                "ASOF",
                optional=True,
            ),
            Ref.keyword("OUTER", optional=True),
        ),
        # This case [ANTI,SEMI,ANY,ASOF] LEFT JOIN
        Sequence(
            OneOf(
                "ANTI",
                "SEMI",
                OneOf("ANY", "ALL", optional=True),
                "ASOF",
            ),
            "LEFT",
        ),
        # This case RIGHT [OUTER,ANTI,SEMI,ANY,ASOF] JOIN
        Sequence(
            "RIGHT",
            OneOf(
                "OUTER",
                "ANTI",
                "SEMI",
                OneOf("ANY", "ALL", optional=True),
                optional=True,
            ),
            Ref.keyword("OUTER", optional=True),
        ),
        # This case [OUTER,ANTI,SEMI,ANY] RIGHT JOIN
        Sequence(
            OneOf(
                "ANTI",
                "SEMI",
                OneOf("ANY", "ALL", optional=True),
                optional=True,
            ),
            "RIGHT",
        ),
        # This case CROSS JOIN
        "CROSS",
        # This case ANY JOIN
        "ANY",
        # This case ALL JOIN
        "ALL",
    ),
    LambdaFunctionSegment=TypedParser("lambda", SymbolSegment, type="lambda"),
)
clickhouse_dialect.replace(
    BinaryOperatorGrammar=OneOf(
        Ref("ArithmeticBinaryOperatorGrammar"),
        Ref("StringBinaryOperatorGrammar"),
        Ref("BooleanBinaryOperatorGrammar"),
        Ref("ComparisonOperatorGrammar"),
        # Add Lambda Function
        Ref("LambdaFunctionSegment"),
    ),
)

clickhouse_dialect.replace(
    JoinLikeClauseGrammar=Sequence(
        AnyNumberOf(
            Ref("ArrayJoinClauseSegment"),
            min_times=1,
        ),
        Ref("AliasExpressionSegment", optional=True),
    ),
)


class BracketedArguments(ansi.BracketedArguments):
    """A series of bracketed arguments.

    e.g. the bracketed part of numeric(1, 3)
    """

    match_grammar = Bracketed(
        Delimited(
            OneOf(
                # Dataypes like Nullable allow optional datatypes here.
                Ref("DatatypeIdentifierSegment"),
                Ref("NumericLiteralSegment"),
            ),
            # The brackets might be empty for some cases...
            optional=True,
        ),
    )


class JoinClauseSegment(ansi.JoinClauseSegment):
    """Any number of join clauses, including the `JOIN` keyword.

    https://clickhouse.com/docs/en/sql-reference/statements/select/join/#supported-types-of-join
    """

    match_grammar = OneOf(
        Sequence(
            Ref("JoinTypeKeywords", optional=True),
            Ref("JoinKeywordsGrammar"),
            Indent,
            Ref("FromExpressionElementSegment"),
            Dedent,
            Conditional(Indent, indented_using_on=True),
            OneOf(
                # ON clause
                Ref("JoinOnConditionSegment"),
                # USING clause
                Sequence(
                    "USING",
                    Conditional(Indent, indented_using_on=False),
                    Delimited(
                        OneOf(
                            Bracketed(
                                Delimited(Ref("SingleIdentifierGrammar")),
                                ephemeral_name="UsingClauseContents",
                            ),
                            Delimited(Ref("SingleIdentifierGrammar")),
                        ),
                    ),
                    Conditional(Dedent, indented_using_on=False),
                ),
                # Requires True for CROSS JOIN
                optional=True,
            ),
            Conditional(Dedent, indented_using_on=True),
        ),
    )


class ArrayJoinClauseSegment(BaseSegment):
    """[LEFT] ARRAY JOIN does not support Join conditions and doesn't work as real JOIN.

    https://clickhouse.com/docs/en/sql-reference/statements/select/array-join
    """

    type = "array_join_clause"

    match_grammar: Matchable = Sequence(
        Ref.keyword("LEFT", optional=True),
        "ARRAY",
        Ref("JoinKeywordsGrammar"),
        Indent,
        Delimited(
            Ref("SelectClauseElementSegment"),
        ),
        Dedent,
    )


class CTEDefinitionSegment(ansi.CTEDefinitionSegment):
    """A CTE Definition from a WITH statement.

    Overridden from ANSI to allow expression CTEs.
    https://clickhouse.com/docs/en/sql-reference/statements/select/with/
    """

    type = "common_table_expression"
    match_grammar: Matchable = OneOf(
        Sequence(
            Ref("SingleIdentifierGrammar"),
            Ref("CTEColumnList", optional=True),
            "AS",
            Bracketed(
                # Ephemeral here to subdivide the query.
                Ref("SelectableGrammar", ephemeral_name="SelectableGrammar")
            ),
        ),
        Sequence(
            Ref("ExpressionSegment"),
            "AS",
            Ref("SingleIdentifierGrammar"),
        ),
    )


class AliasExpressionSegment(ansi.AliasExpressionSegment):
    """A reference to an object with an `AS` clause."""

    type = "alias_expression"
    match_grammar: Matchable = Sequence(
        Indent,
        Ref.keyword("AS", optional=True),
        OneOf(
            Sequence(
                Ref("SingleIdentifierGrammar"),
                # Column alias in VALUES clause
                Bracketed(Ref("SingleIdentifierListSegment"), optional=True),
            ),
            Ref("SingleQuotedIdentifierSegment"),
            exclude=OneOf(
                "LATERAL",
                "WINDOW",
                "KEYS",
            ),
        ),
        Dedent,
    )


class FromExpressionElementSegment(ansi.FromExpressionElementSegment):
    """A table expression.

    Overridden from ANSI to allow FINAL modifier.
    https://clickhouse.com/docs/en/sql-reference/statements/select/from#final-modifier
    """

    type = "from_expression_element"
    match_grammar: Matchable = Sequence(
        Ref("PreTableFunctionKeywordsGrammar", optional=True),
        OptionallyBracketed(Ref("TableExpressionSegment")),
        Ref(
            "AliasExpressionSegment",
            exclude=OneOf(
                Ref("SamplingExpressionSegment"),
                Ref("JoinLikeClauseGrammar"),
                "FINAL",
                Ref("JoinClauseSegment"),
            ),
            optional=True,
        ),
        Ref.keyword("FINAL", optional=True),
        # https://cloud.google.com/bigquery/docs/reference/standard-sql/arrays#flattening_arrays
        Sequence("WITH", "OFFSET", Ref("AliasExpressionSegment"), optional=True),
        Ref("SamplingExpressionSegment", optional=True),
        Ref("PostTableExpressionGrammar", optional=True),
    )


class EngineFunctionSegment(BaseSegment):
    """A ClickHouse `ENGINE` clause function.

    With this segment we attempt to match all possible engines.
    """

    type = "engine_function"
    match_grammar: Matchable = Sequence(
        Sequence(
            Ref(
                "FunctionNameSegment",
                exclude=OneOf(
                    Ref("DatePartFunctionNameSegment"),
                    Ref("ValuesClauseSegment"),
                ),
            ),
            Bracketed(
                Ref(
                    "FunctionContentsGrammar",
                    # The brackets might be empty for some functions...
                    optional=True,
                    ephemeral_name="FunctionContentsGrammar",
                ),
                # Engine functions may omit brackets.
                optional=True,
            ),
        ),
    )


class EngineSegment(BaseSegment):
    """An `ENGINE` used in `CREATE TABLE`."""

    type = "engine"

    match_grammar = Sequence(
        "ENGINE",
        Ref("EqualsSegment"),
        Sequence(
            Ref("EngineFunctionSegment"),
            AnySetOf(
                Sequence(
                    "ORDER",
                    "BY",
                    OneOf(
                        Ref("BracketedColumnReferenceListGrammar"),
                        Ref("ColumnReferenceSegment"),
                    ),
                    optional=True,
                ),
                Sequence(
                    "PARTITION",
                    "BY",
                    Ref("ExpressionSegment"),
                    optional=True,
                ),
                Sequence(
                    "PRIMARY",
                    "KEY",
                    Ref("ExpressionSegment"),
                    optional=True,
                ),
                Sequence(
                    "SAMPLE",
                    "BY",
                    Ref("ExpressionSegment"),
                    optional=True,
                ),
                Sequence(
                    "SETTINGS",
                    Delimited(
                        AnyNumberOf(
                            Sequence(
                                Ref("NakedIdentifierSegment"),
                                Ref("EqualsSegment"),
                                OneOf(
                                    Ref("NumericLiteralSegment"),
                                    Ref("QuotedLiteralSegment"),
                                ),
                                optional=True,
                            ),
                        )
                    ),
                    optional=True,
                ),
            ),
        ),
    )


class ColumnTTLSegment(BaseSegment):
    """A TTL clause for columns as used in CREATE TABLE.

    Specified in
    https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree/#mergetree-column-ttl
    """

    type = "column_ttl_segment"

    match_grammar = Sequence(
        "TTL",
        Ref("ExpressionSegment"),
    )


class TableTTLSegment(BaseSegment):
    """A TTL clause for tables as used in CREATE TABLE.

    Specified in
    https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree/#mergetree-table-ttl
    """

    type = "table_ttl_segment"

    match_grammar = Sequence(
        "TTL",
        Delimited(
            Sequence(
                Ref("ExpressionSegment"),
                OneOf(
                    "DELETE",
                    Sequence(
                        "TO",
                        "VOLUME",
                        Ref("QuotedLiteralSegment"),
                    ),
                    Sequence(
                        "TO",
                        "DISK",
                        Ref("QuotedLiteralSegment"),
                    ),
                    optional=True,
                ),
                Ref("WhereClauseSegment", optional=True),
                Ref("GroupByClauseSegment", optional=True),
            )
        ),
    )


class ColumnConstraintSegment(BaseSegment):
    """ClickHouse specific column constraints.

    As specified in
    https://clickhouse.com/docs/en/sql-reference/statements/create/table#constraints
    """

    type = "column_constraint_segment"

    match_grammar = AnySetOf(
        Sequence(
            Sequence(
                "CONSTRAINT",
                Ref("ObjectReferenceSegment"),
                optional=True,
            ),
            OneOf(
                Sequence(Ref.keyword("NOT", optional=True), "NULL"),
                Sequence("CHECK", Bracketed(Ref("ExpressionSegment"))),
                Sequence(
                    OneOf(
                        "DEFAULT",
                        "MATERIALIZED",
                        "ALIAS",
                    ),
                    OneOf(
                        Ref("LiteralGrammar"),
                        Ref("FunctionSegment"),
                        Ref("BareFunctionSegment"),
                    ),
                ),
                Sequence(
                    "EPHEMERAL",
                    OneOf(
                        Ref("LiteralGrammar"),
                        Ref("FunctionSegment"),
                        Ref("BareFunctionSegment"),
                        optional=True,
                    ),
                ),
                Ref("PrimaryKeyGrammar"),
                Sequence(
                    "CODEC",
                    Ref("FunctionContentsGrammar"),
                    optional=True,
                ),
                Ref("ColumnTTLSegment"),
            ),
        )
    )


class CreateTableStatementSegment(ansi.CreateTableStatementSegment):
    """A `CREATE TABLE` statement.

    As specified in
    https://clickhouse.com/docs/en/sql-reference/statements/create/table/
    """

    type = "create_table_statement"

    match_grammar: Matchable = Sequence(
        "CREATE",
        OneOf(
            Ref("OrReplaceGrammar"),
            Ref.keyword("TEMPORARY"),
            optional=True,
        ),
        "TABLE",
        Ref("IfNotExistsGrammar", optional=True),
        Ref("TableReferenceSegment"),
        Sequence(
            "ON",
            "CLUSTER",
            Ref("ExpressionSegment"),
            optional=True,
        ),
        OneOf(
            # CREATE TABLE (...):
            Sequence(
                Bracketed(
                    Delimited(
                        OneOf(
                            Ref("TableConstraintSegment"),
                            Ref("ColumnDefinitionSegment"),
                            Ref("ColumnConstraintSegment"),
                        ),
                    ),
                    # Column definition may be missing if using AS SELECT
                    optional=True,
                ),
                Ref("EngineSegment"),
                # CREATE TABLE (...) AS SELECT:
                Sequence(
                    "AS",
                    Ref("SelectableGrammar"),
                    optional=True,
                ),
            ),
            # CREATE TABLE AS other_table:
            Sequence(
                "AS",
                Ref("TableReferenceSegment"),
                Ref("EngineSegment", optional=True),
            ),
            # CREATE TABLE AS table_function():
            Sequence(
                "AS",
                Ref("FunctionSegment"),
            ),
        ),
        AnySetOf(
            Sequence(
                "COMMENT",
                Ref("SingleQuotedIdentifierSegment"),
            ),
            Ref("TableTTLSegment"),
            optional=True,
        ),
        Ref("TableEndClauseSegment", optional=True),
    )


class CreateMaterializedViewStatementSegment(BaseSegment):
    """A `CREATE MATERIALIZED VIEW` statement.

    https://clickhouse.com/docs/en/sql-reference/statements/create/table/
    """

    type = "create_materialized_view_statement"

    match_grammar = Sequence(
        "CREATE",
        "MATERIALIZED",
        "VIEW",
        Ref("IfNotExistsGrammar", optional=True),
        Ref("TableReferenceSegment"),
        Sequence(
            "ON",
            "CLUSTER",
            Ref("ExpressionSegment"),
            optional=True,
        ),
        OneOf(
            Sequence(
                "TO",
                Ref("TableReferenceSegment"),
                Ref("EngineSegment", optional=True),
            ),
            Sequence(
                Ref("EngineSegment", optional=True),
                Sequence("POPULATE", optional=True),
            ),
        ),
        "AS",
        Ref("SelectableGrammar"),
        Ref("TableEndClauseSegment", optional=True),
    )


class StatementSegment(ansi.StatementSegment):
    """Overriding StatementSegment to allow for additional segment parsing."""

    match_grammar = ansi.StatementSegment.match_grammar
    parse_grammar = ansi.StatementSegment.parse_grammar.copy(
        insert=[
            Ref("CreateTableStatementSegment"),
            Ref("CreateMaterializedViewStatementSegment"),
        ]
    )
