-- ====================================================================
-- AGENT AUDIT LOG SCRIPT (Phase 5a)
-- Purpose: an append-only trail of everything the agent does, enforced by SQL Server itself.
--
-- Run with:  make audit-db-setup
-- sqlcmd variables (passed with -v, never hard-coded here):
--   DbName          target database, e.g. SupplyChainDb
--   WriterPassword  password for the USR_FDE_AUDIT_W login (INSERT only), no single quotes
--   ReaderPassword  password for the USR_FDE_AUDIT_R login (SELECT only), no single quotes
--
-- Three identities, least privilege:
--   USR_FDE_RO        (Phase 3b) the agent's SQL tool. DENIED everything on schema [audit].
--   USR_FDE_AUDIT_W   writes audit rows. INSERT on the one table, nothing else.
--   USR_FDE_AUDIT_R   reads audit rows (the UI viewer). SELECT on the one table, nothing else.
--
-- Idempotent: safe to re-run. Re-running also rotates both passwords.
-- Accepted limit: the database owner (dbo/sa) can still edit the table.
-- ====================================================================

:on error exit

-- 0. Server-level logins (logins live in master; users live in a database)
USE [master];
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'USR_FDE_AUDIT_W')
    CREATE LOGIN [USR_FDE_AUDIT_W] WITH PASSWORD = N'$(WriterPassword)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN [USR_FDE_AUDIT_W] WITH PASSWORD = N'$(WriterPassword)';

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'USR_FDE_AUDIT_R')
    CREATE LOGIN [USR_FDE_AUDIT_R] WITH PASSWORD = N'$(ReaderPassword)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN [USR_FDE_AUDIT_R] WITH PASSWORD = N'$(ReaderPassword)';
GO

USE [$(DbName)];
GO

-- 1. A schema of its own. NOT [ai]: [ai] is the agent-facing, read-only layer.
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'audit')
    EXEC (N'CREATE SCHEMA [audit] AUTHORIZATION [dbo]');
GO

-- 2. The table: one row per event, so a turn can be replayed in order.
IF OBJECT_ID(N'[audit].[AgentAuditLog]', N'U') IS NULL
BEGIN
    CREATE TABLE [audit].[AgentAuditLog] (
        [LogId]            BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT [PK_AgentAuditLog] PRIMARY KEY,
        [LoggedAtUtc]      DATETIME2(3)  NOT NULL CONSTRAINT [DF_AgentAuditLog_LoggedAtUtc] DEFAULT SYSUTCDATETIME(),
        [SessionId]        VARCHAR(64)   NOT NULL,   -- the chat (LangGraph thread_id)
        [TurnId]           VARCHAR(64)   NOT NULL,   -- one user question and everything it triggered
        [StepNo]           INT           NOT NULL,   -- order of events inside the turn
        [Node]             VARCHAR(20)   NOT NULL,   -- user | reasoner | tools | graph
        [EventType]        VARCHAR(20)   NOT NULL,   -- user_prompt | tool_call | tool_result | final_answer | error
        [ToolName]         VARCHAR(100)  NULL,
        [ToolCallId]       VARCHAR(100)  NULL,       -- links a tool_call to its tool_result
        [Status]           VARCHAR(20)   NULL,       -- ok | error | unavailable (tool results)
        [InputData]        NVARCHAR(MAX) NULL,       -- the prompt, tool arguments or SQL
        [OutputData]       NVARCHAR(MAX) NULL,       -- the model reply or the tool result, in full
        [ErrorMessage]     NVARCHAR(MAX) NULL,       -- secrets redacted by the writer
        [Provider]         VARCHAR(50)   NULL,
        [Model]            VARCHAR(100)  NULL,
        [DurationMs]       INT           NULL,       -- time since the previous event
        [PromptTokens]     INT           NULL,
        [CompletionTokens] INT           NULL
    );
    CREATE INDEX [IX_AgentAuditLog_Session] ON [audit].[AgentAuditLog] ([SessionId], [LogId]);
    CREATE INDEX [IX_AgentAuditLog_LoggedAt] ON [audit].[AgentAuditLog] ([LoggedAtUtc]);
END
GO

-- 3. Database users for the two logins.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_AUDIT_W')
    CREATE USER [USR_FDE_AUDIT_W] FOR LOGIN [USR_FDE_AUDIT_W];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_AUDIT_R')
    CREATE USER [USR_FDE_AUDIT_R] FOR LOGIN [USR_FDE_AUDIT_R];
GO

-- 4. Grants: one object, one verb each. Everything else is denied explicitly.
GRANT INSERT ON OBJECT::[audit].[AgentAuditLog] TO [USR_FDE_AUDIT_W];
DENY SELECT, UPDATE, DELETE, ALTER, REFERENCES ON OBJECT::[audit].[AgentAuditLog] TO [USR_FDE_AUDIT_W];

GRANT SELECT ON OBJECT::[audit].[AgentAuditLog] TO [USR_FDE_AUDIT_R];
DENY INSERT, UPDATE, DELETE, ALTER, REFERENCES ON OBJECT::[audit].[AgentAuditLog] TO [USR_FDE_AUDIT_R];

-- Neither audit login has any business in the data schemas.
DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[dbo] TO [USR_FDE_AUDIT_W], [USR_FDE_AUDIT_R];
DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[ai]  TO [USR_FDE_AUDIT_W], [USR_FDE_AUDIT_R];
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION, CREATE SCHEMA TO [USR_FDE_AUDIT_W], [USR_FDE_AUDIT_R];

-- The agent can neither read nor edit its own trail.
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_RO')
    DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[audit] TO [USR_FDE_RO];
GO

-- 5. Self-check: act as each login and prove the boundary holds.
--    Every line must print PASS; any FAIL aborts the script.
SET NOCOUNT ON;
DECLARE @failed BIT = 0;

-- ---- the writer: INSERT only
EXECUTE AS USER = N'USR_FDE_AUDIT_W';
BEGIN TRY
    INSERT INTO [audit].[AgentAuditLog] ([SessionId], [TurnId], [StepNo], [Node], [EventType])
    VALUES ('__self_check__', '__self_check__', 0, 'graph', 'self_check');
    PRINT 'PASS  writer can INSERT';
END TRY
BEGIN CATCH PRINT 'FAIL  writer cannot INSERT: ' + ERROR_MESSAGE(); SET @failed = 1; END CATCH;

BEGIN TRY
    EXEC (N'SELECT TOP 1 * FROM [audit].[AgentAuditLog]');
    PRINT 'FAIL  writer can SELECT'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  writer SELECT denied'; END CATCH;

BEGIN TRY
    EXEC (N'UPDATE [audit].[AgentAuditLog] SET [Status] = ''x''');
    PRINT 'FAIL  writer can UPDATE'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  writer UPDATE denied'; END CATCH;

BEGIN TRY
    EXEC (N'DELETE FROM [audit].[AgentAuditLog]');
    PRINT 'FAIL  writer can DELETE'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  writer DELETE denied'; END CATCH;

BEGIN TRY
    EXEC (N'SELECT TOP 1 * FROM [ai].[VW_SUPPLY_CHAIN_READINGS]');
    PRINT 'FAIL  writer can read the data view'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  writer cannot read the data view'; END CATCH;
REVERT;

-- ---- the reader: SELECT only
EXECUTE AS USER = N'USR_FDE_AUDIT_R';
BEGIN TRY
    DECLARE @n BIGINT = (SELECT COUNT_BIG(*) FROM [audit].[AgentAuditLog]);
    PRINT CASE WHEN @n >= 1 THEN 'PASS  reader can SELECT' ELSE 'FAIL  reader sees no rows' END;
    IF @n < 1 SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'FAIL  reader cannot SELECT: ' + ERROR_MESSAGE(); SET @failed = 1; END CATCH;

BEGIN TRY
    EXEC (N'INSERT INTO [audit].[AgentAuditLog] ([SessionId],[TurnId],[StepNo],[Node],[EventType]) VALUES (''x'',''x'',0,''x'',''x'')');
    PRINT 'FAIL  reader can INSERT'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  reader INSERT denied'; END CATCH;

BEGIN TRY
    EXEC (N'DELETE FROM [audit].[AgentAuditLog]');
    PRINT 'FAIL  reader can DELETE'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  reader DELETE denied'; END CATCH;
REVERT;

-- ---- the agent's own login: no access at all
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_RO')
BEGIN
    EXECUTE AS USER = N'USR_FDE_RO';
    BEGIN TRY
        EXEC (N'SELECT TOP 1 * FROM [audit].[AgentAuditLog]');
        PRINT 'FAIL  the agent login can read the audit log'; SET @failed = 1;
    END TRY
    BEGIN CATCH PRINT 'PASS  agent login cannot read the audit log'; END CATCH;
    BEGIN TRY
        EXEC (N'INSERT INTO [audit].[AgentAuditLog] ([SessionId],[TurnId],[StepNo],[Node],[EventType]) VALUES (''x'',''x'',0,''x'',''x'')');
        PRINT 'FAIL  the agent login can write the audit log'; SET @failed = 1;
    END TRY
    BEGIN CATCH PRINT 'PASS  agent login cannot write the audit log'; END CATCH;
    REVERT;
END

-- Remove the probe row (as the owner; the writer cannot delete).
DELETE FROM [audit].[AgentAuditLog] WHERE [SessionId] = '__self_check__';

IF @failed = 1
    RAISERROR (N'Audit security self-check failed.', 16, 1);
ELSE
    PRINT 'All audit security checks passed.';
GO
