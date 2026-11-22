-- Run by ./setup.sh init. Safe to re-run.
-- sqlcmd variables: DB_NAME, READER_USER, READER_PASSWORD, LIBRARIAN_USER, LIBRARIAN_PASSWORD
SET NOCOUNT ON;

IF DB_ID(N'$(DB_NAME)') IS NULL
    CREATE DATABASE [$(DB_NAME)];
GO

-- Server logins
IF SUSER_ID(N'$(READER_USER)') IS NULL
    CREATE LOGIN [$(READER_USER)] WITH PASSWORD = N'$(READER_PASSWORD)', CHECK_POLICY = ON, DEFAULT_DATABASE = [$(DB_NAME)];
ELSE
    ALTER LOGIN [$(READER_USER)] WITH PASSWORD = N'$(READER_PASSWORD)';

IF SUSER_ID(N'$(LIBRARIAN_USER)') IS NULL
    CREATE LOGIN [$(LIBRARIAN_USER)] WITH PASSWORD = N'$(LIBRARIAN_PASSWORD)', CHECK_POLICY = ON, DEFAULT_DATABASE = [$(DB_NAME)];
ELSE
    ALTER LOGIN [$(LIBRARIAN_USER)] WITH PASSWORD = N'$(LIBRARIAN_PASSWORD)';
GO

USE [$(DB_NAME)];
GO

IF SCHEMA_ID(N'staging') IS NULL EXEC (N'CREATE SCHEMA staging');
GO

IF USER_ID(N'$(READER_USER)') IS NULL CREATE USER [$(READER_USER)] FOR LOGIN [$(READER_USER)];
IF USER_ID(N'$(LIBRARIAN_USER)') IS NULL CREATE USER [$(LIBRARIAN_USER)] FOR LOGIN [$(LIBRARIAN_USER)];
ALTER ROLE db_datareader ADD MEMBER [$(READER_USER)];
ALTER ROLE db_datareader ADD MEMBER [$(LIBRARIAN_USER)];
ALTER ROLE db_datawriter ADD MEMBER [$(LIBRARIAN_USER)];
DENY SELECT ON SCHEMA::staging TO [$(READER_USER)];
GO

-- Mirror run history
IF OBJECT_ID(N'dbo._mirror_log') IS NULL
    CREATE TABLE dbo._mirror_log (
        id         int IDENTITY PRIMARY KEY,
        run_at     datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
        table_name nvarchar(256) NOT NULL,
        row_count  int NOT NULL,
        status     nvarchar(50) NOT NULL
    );
GO

-- Blank template with the columns every Celestial table shares.
-- Altium's DbLib keys on [Part Number].
IF OBJECT_ID(N'dbo.[$(CUSTOM_PREFIX)Template]') IS NULL
    CREATE TABLE dbo.[$(CUSTOM_PREFIX)Template] (
        [Part Number]      varchar(255) NOT NULL PRIMARY KEY,
        [Comment]          varchar(255) NULL,
        [Description]      varchar(1000) NULL,
        [Value]            varchar(255) NULL,
        [Library Ref]      varchar(255) NOT NULL,
        [Library Path]     varchar(255) NOT NULL,
        [Footprint Ref]    varchar(255) NOT NULL,
        [Footprint Path]   varchar(255) NOT NULL,
        [Footprint Ref 2]  varchar(255) NULL,
        [Footprint Path 2] varchar(255) NULL,
        [Footprint Ref 3]  varchar(255) NULL,
        [Footprint Path 3] varchar(255) NULL,
        [Manufacturer 1]             varchar(255) NULL,
        [Manufacturer Part Number 1] varchar(255) NULL,
        [Supplier 1]                 varchar(255) NULL,
        [Supplier Part Number 1]     varchar(255) NULL,
        [ComponentLink1Description]  varchar(255) NULL,
        [ComponentLink1URL]          varchar(1000) NULL
    );
GO

-- EXEC dbo.make_custom_table @like = N'Capacitors - Ceramic';
--   -> creates [Custom - Capacitors - Ceramic] with the same columns, no rows.
-- EXEC dbo.make_custom_table @name = N'Connectors - Detex';
--   -> copies the template's columns.
CREATE OR ALTER PROCEDURE dbo.make_custom_table
    @name sysname = NULL,
    @like sysname = NULL
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @prefix nvarchar(50) = N'$(CUSTOM_PREFIX)';
    DECLARE @src sysname = COALESCE(@like, @prefix + N'Template');
    DECLARE @dst sysname = @prefix + COALESCE(@name, @like);

    IF @dst IS NULL OR @dst = @prefix
        THROW 50001, 'Pass @name, @like, or both.', 1;
    IF OBJECT_ID(QUOTENAME(N'dbo') + N'.' + QUOTENAME(@src)) IS NULL
        THROW 50002, 'Source table not found.', 1;
    IF OBJECT_ID(QUOTENAME(N'dbo') + N'.' + QUOTENAME(@dst)) IS NOT NULL
        THROW 50003, 'Target table already exists.', 1;

    DECLARE @sql nvarchar(max) =
        N'SELECT TOP (0) * INTO dbo.' + QUOTENAME(@dst) + N' FROM dbo.' + QUOTENAME(@src) + N';';
    EXEC (@sql);

    -- Index the key column if it's indexable (not a MAX type).
    IF EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID(N'dbo.' + QUOTENAME(@dst))
                 AND name = N'Part Number' AND max_length BETWEEN 1 AND 900)
    BEGIN
        SET @sql = N'CREATE UNIQUE INDEX UX_PartNumber ON dbo.' + QUOTENAME(@dst) + N' ([Part Number]);';
        EXEC (@sql);
    END

    SELECT @dst AS created_table;
END;
GO

GRANT EXECUTE ON dbo.make_custom_table TO [$(LIBRARIAN_USER)];
GO

-- Hide housekeeping tables from Altium's login so they never show up in the DbLib.
DENY SELECT ON dbo._mirror_log TO [$(READER_USER)];
DENY SELECT ON dbo.[$(CUSTOM_PREFIX)Template] TO [$(READER_USER)];
GO

PRINT 'Init complete.';

