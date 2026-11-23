-- Drop Celestial's raw z_ tables from your copy. Run by ./setup.sh drop-raw (as sa). Safe to re-run.
-- sqlcmd variables: DB_NAME
--
-- The z_ tables (z_CapacitorsCeramic, ...) are the raw source Celestial builds its category
-- views from. Altium has them disabled, the Parts Manager ignores them, and the category
-- tables already hold the same rows. The mirror no longer copies them, so this only needs
-- to run once on a server that was mirrored before that change.
--
-- Drops nothing if a view, procedure, function, trigger or foreign key outside the z_
-- tables still references one of them. (References built inside dynamic SQL strings
-- can't be detected.) Everything is dropped in one transaction: all or nothing.
--
-- Getting them back: ./setup.sh mirror --include-raw --only z_
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

USE [$(DB_NAME)];
GO

DECLARE @raw TABLE (object_id int PRIMARY KEY, name sysname NOT NULL);
INSERT @raw (object_id, name)
SELECT object_id, name FROM sys.tables
WHERE schema_id = SCHEMA_ID(N'dbo') AND name LIKE N'z[_]%';

DECLARE @n int = (SELECT COUNT(*) FROM @raw);
IF @n = 0
BEGIN
    PRINT N'No z_ tables in dbo. Nothing to do.';
    RETURN;
END;

-- Anything outside the z_ tables that still uses one of them.
DECLARE @users TABLE (used_by nvarchar(600) NOT NULL, z_table sysname NOT NULL);
INSERT @users (used_by, z_table)
SELECT DISTINCT
       o.type_desc COLLATE DATABASE_DEFAULT + N' ' + QUOTENAME(OBJECT_SCHEMA_NAME(o.object_id)) + N'.' + QUOTENAME(o.name),
       d.referenced_entity_name
FROM sys.sql_expression_dependencies d
JOIN sys.objects o ON o.object_id = d.referencing_id
WHERE d.referenced_entity_name LIKE N'z[_]%'
  AND ISNULL(d.referenced_schema_name, N'dbo') = N'dbo'
  AND d.referencing_id NOT IN (SELECT object_id FROM @raw)
  AND ISNULL(o.parent_object_id, 0) NOT IN (SELECT object_id FROM @raw);  -- triggers on z_ tables go with them
INSERT @users (used_by, z_table)
SELECT N'FOREIGN KEY ' + QUOTENAME(fk.name) + N' on dbo.' + QUOTENAME(OBJECT_NAME(fk.parent_object_id)),
       r.name
FROM sys.foreign_keys fk
JOIN @raw r ON r.object_id = fk.referenced_object_id
WHERE fk.parent_object_id NOT IN (SELECT object_id FROM @raw);

IF EXISTS (SELECT 1 FROM @users)
BEGIN
    SELECT used_by, z_table FROM @users ORDER BY used_by, z_table;
    THROW 50010, N'Nothing dropped: the objects listed above still use z_ tables. Drop or change them first.', 1;
END;

DECLARE @sql nvarchar(max) = N'';
-- Foreign keys between z_ tables first, so the drop order doesn't matter.
SELECT @sql += N'ALTER TABLE dbo.' + QUOTENAME(OBJECT_NAME(fk.parent_object_id))
             + N' DROP CONSTRAINT ' + QUOTENAME(fk.name) + N';' + NCHAR(10)
FROM sys.foreign_keys fk
WHERE fk.parent_object_id IN (SELECT object_id FROM @raw);
SELECT @sql += N'DROP TABLE dbo.' + QUOTENAME(name) + N';' + NCHAR(10)
FROM @raw;

BEGIN TRANSACTION;
EXEC sys.sp_executesql @sql;
COMMIT TRANSACTION;

PRINT CONCAT(N'Dropped ', @n, N' z_ tables. The space is free for the database to reuse; the data file keeps its size.');
GO
