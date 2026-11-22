-- Parts Manager (partsui) additions. Run by ./setup.sh init after 01_init.sql. Safe to re-run.
-- sqlcmd variables: DB_NAME, READER_USER, LIBRARIAN_USER, CUSTOM_PREFIX
--
--   dbo._parts_index            flat search index over every part table (hidden from Altium)
--   dbo.refresh_parts_index     rebuild the whole index (the mirror calls it after each run)
--   dbo.upsert_parts_index_row  re-index one part after a save or delete
--
-- Indexed: every dbo table or view with a [Part Number] column, except raw Celestial z_ tables,
-- housekeeping tables starting with _, and [<prefix>Template]. Only tables can be "custom".
SET NOCOUNT ON;
GO

USE [$(DB_NAME)];
GO

IF OBJECT_ID(N'dbo._parts_index', N'U') IS NULL
BEGIN
    CREATE TABLE dbo._parts_index (
        table_name    nvarchar(256)  NOT NULL,
        part_number   varchar(900)   NOT NULL,
        comment       nvarchar(400)  NULL,
        description   nvarchar(1000) NULL,
        library_ref   nvarchar(400)  NULL,
        footprint_ref nvarchar(400)  NULL,
        is_custom     bit            NOT NULL
    );
    CREATE CLUSTERED INDEX CIX_parts_index_part_number ON dbo._parts_index (part_number);
    CREATE NONCLUSTERED INDEX IX_parts_index_table_name ON dbo._parts_index (table_name);
END;
GO

CREATE OR ALTER PROCEDURE dbo.refresh_parts_index
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @prefix nvarchar(50) = N'$(CUSTOM_PREFIX)';

    CREATE TABLE #idx (
        table_name    nvarchar(256)  NOT NULL,
        part_number   varchar(900)   NOT NULL,
        comment       nvarchar(400)  NULL,
        description   nvarchar(1000) NULL,
        library_ref   nvarchar(400)  NULL,
        footprint_ref nvarchar(400)  NULL,
        is_custom     bit            NOT NULL
    );

    -- One INSERT per source table, run as a single batch. Optional columns that a table
    -- lacks become NULL. Names are quoted with QUOTENAME; no values are concatenated.
    DECLARE @sql nvarchar(max);
    SELECT @sql = STRING_AGG(CAST(
             N'INSERT INTO #idx SELECT N' + QUOTENAME(o.name, N'''')
           + N', CAST([Part Number] AS varchar(900)), '
           + CASE WHEN COL_LENGTH(N'dbo.' + QUOTENAME(o.name), N'Comment') IS NULL THEN N'NULL'
                  ELSE N'LEFT(CAST([Comment] AS nvarchar(max)), 400)' END + N', '
           + CASE WHEN COL_LENGTH(N'dbo.' + QUOTENAME(o.name), N'Description') IS NULL THEN N'NULL'
                  ELSE N'LEFT(CAST([Description] AS nvarchar(max)), 1000)' END + N', '
           + CASE WHEN COL_LENGTH(N'dbo.' + QUOTENAME(o.name), N'Library Ref') IS NULL THEN N'NULL'
                  ELSE N'LEFT(CAST([Library Ref] AS nvarchar(max)), 400)' END + N', '
           + CASE WHEN COL_LENGTH(N'dbo.' + QUOTENAME(o.name), N'Footprint Ref') IS NULL THEN N'NULL'
                  ELSE N'LEFT(CAST([Footprint Ref] AS nvarchar(max)), 400)' END + N', '
           + CASE WHEN o.type = 'U' AND LEFT(o.name, DATALENGTH(@prefix) / 2) = @prefix
                  THEN N'1' ELSE N'0' END
           + N' FROM dbo.' + QUOTENAME(o.name) + N' WHERE [Part Number] IS NOT NULL;'
           AS nvarchar(max)), NCHAR(10))
    FROM sys.objects o
    WHERE o.schema_id = SCHEMA_ID(N'dbo')
      AND o.type IN ('U', 'V')
      AND o.is_ms_shipped = 0
      AND o.name NOT LIKE N'z[_]%'
      AND o.name NOT LIKE N'[_]%'
      AND o.name <> @prefix + N'Template'
      AND COL_LENGTH(N'dbo.' + QUOTENAME(o.name), N'Part Number') IS NOT NULL;

    IF @sql IS NOT NULL
        EXEC sys.sp_executesql @sql;

    -- Swap in one short transaction, so searches never see a half-built index.
    BEGIN TRANSACTION;
        TRUNCATE TABLE dbo._parts_index;
        INSERT INTO dbo._parts_index WITH (TABLOCK)
            (table_name, part_number, comment, description, library_ref, footprint_ref, is_custom)
        SELECT table_name, part_number, comment, description, library_ref, footprint_ref, is_custom
        FROM #idx;
    COMMIT;

    SELECT COUNT(*) AS indexed_rows,
           ISNULL(SUM(CAST(is_custom AS int)), 0) AS custom_rows
    FROM dbo._parts_index;
END;
GO

-- Re-index one part: drop the index rows for (@table, @part_number), then re-read that part
-- from @table. Call it after an insert, update or delete (for a key change: old and new key).
CREATE OR ALTER PROCEDURE dbo.upsert_parts_index_row
    @table sysname,
    @part_number varchar(900)
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @prefix nvarchar(50) = N'$(CUSTOM_PREFIX)';
    DECLARE @obj nvarchar(300) = N'dbo.' + QUOTENAME(@table);
    DECLARE @type char(2) = (SELECT type FROM sys.objects WHERE object_id = OBJECT_ID(@obj));

    IF @table IS NULL OR ISNULL(@type, '') NOT IN ('U', 'V')
       OR @table LIKE N'z[_]%' OR @table LIKE N'[_]%' OR @table = @prefix + N'Template'
       OR COL_LENGTH(@obj, N'Part Number') IS NULL
        THROW 50010, 'Not an indexed part table.', 1;

    DECLARE @sql nvarchar(max) =
          N'INSERT INTO dbo._parts_index '
        + N'(table_name, part_number, comment, description, library_ref, footprint_ref, is_custom) '
        + N'SELECT @tbl, CAST([Part Number] AS varchar(900)), '
        + CASE WHEN COL_LENGTH(@obj, N'Comment') IS NULL THEN N'NULL'
               ELSE N'LEFT(CAST([Comment] AS nvarchar(max)), 400)' END + N', '
        + CASE WHEN COL_LENGTH(@obj, N'Description') IS NULL THEN N'NULL'
               ELSE N'LEFT(CAST([Description] AS nvarchar(max)), 1000)' END + N', '
        + CASE WHEN COL_LENGTH(@obj, N'Library Ref') IS NULL THEN N'NULL'
               ELSE N'LEFT(CAST([Library Ref] AS nvarchar(max)), 400)' END + N', '
        + CASE WHEN COL_LENGTH(@obj, N'Footprint Ref') IS NULL THEN N'NULL'
               ELSE N'LEFT(CAST([Footprint Ref] AS nvarchar(max)), 400)' END + N', '
        + CASE WHEN @type = 'U' AND LEFT(@table, DATALENGTH(@prefix) / 2) = @prefix
               THEN N'1' ELSE N'0' END
        + N' FROM ' + @obj + N' WHERE [Part Number] = @pn;';

    BEGIN TRANSACTION;
        DELETE FROM dbo._parts_index WHERE table_name = @table AND part_number = @part_number;
        EXEC sys.sp_executesql @sql, N'@tbl nvarchar(256), @pn varchar(900)',
             @tbl = @table, @pn = @part_number;
    COMMIT;
END;
GO

GRANT EXECUTE ON dbo.refresh_parts_index TO [$(LIBRARIAN_USER)];
GRANT EXECUTE ON dbo.upsert_parts_index_row TO [$(LIBRARIAN_USER)];
GO

-- Hide the index from Altium's login, like the other housekeeping tables.
DENY SELECT ON dbo._parts_index TO [$(READER_USER)];
GO

-- Build the index now, so search works straight after an upgrade on a server that already
-- holds Celestial data. After this, the mirror and the app's saves keep it current.
BEGIN TRY
    EXEC dbo.refresh_parts_index;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK;
    PRINT 'Warning: could not build the parts search index: ' + ERROR_MESSAGE()
        + ' Use Re-index on the Parts Manager status page once it is fixed.';
END CATCH;
GO

PRINT 'Parts Manager objects ready.';
