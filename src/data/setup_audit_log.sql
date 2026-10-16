
-- 0. Server-level logins (logins live in master; users live in a database)
USE [master];
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'USR_FDE_AUDIT_W_2')
    CREATE LOGIN [USR_FDE_AUDIT_W_2] WITH PASSWORD = N'$(WriterPassword)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN [USR_FDE_AUDIT_W_2] WITH PASSWORD = N'$(WriterPassword)';

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'USR_FDE_AUDIT_R_2')
    CREATE LOGIN [USR_FDE_AUDIT_R_2] WITH PASSWORD = N'$(ReaderPassword)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN [USR_FDE_AUDIT_R_2] WITH PASSWORD = N'$(ReaderPassword)';
GO

USE [$(DbName)];
GO

-- 1. A schema of its own. NOT [ai]: [ai] is the agent-facing, read-only layer.
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'audit_2')
    EXEC (N'CREATE SCHEMA [audit_2] AUTHORIZATION [dbo]');
GO

-- 2. The table: one row per event, so a turn can be replayed in order.
IF OBJECT_ID(N'[audit_2].[AgentAuditLog]', N'U') IS NULL
BEGIN
    CREATE TABLE [audit_2].[AgentAuditLog] (
        [LogId]            BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT [PK_AgentAuditLog] PRIMARY KEY, -- identity will auto-increment
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
    CREATE INDEX [IX_AgentAuditLog_Session] ON [audit_2].[AgentAuditLog] ([SessionId], [LogId]);
    CREATE INDEX [IX_AgentAuditLog_LoggedAt] ON [audit_2].[AgentAuditLog] ([LoggedAtUtc]);
END
GO

-- 3. Database users for the two logins.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_AUDIT_W_2')
    CREATE USER [USR_FDE_AUDIT_W_2] FOR LOGIN [USR_FDE_AUDIT_W_2];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_AUDIT_R_2')
    CREATE USER [USR_FDE_AUDIT_R_2] FOR LOGIN [USR_FDE_AUDIT_R_2];
GO

-- 4. Grants: one object, one verb each. Everything else is denied explicitly.
GRANT INSERT ON OBJECT::[audit_2].[AgentAuditLog] TO [USR_FDE_AUDIT_W_2];
DENY SELECT, UPDATE, DELETE, ALTER, REFERENCES ON OBJECT::[audit_2].[AgentAuditLog] TO [USR_FDE_AUDIT_W_2];

GRANT SELECT ON OBJECT::[audit_2].[AgentAuditLog] TO [USR_FDE_AUDIT_R_2];
DENY INSERT, UPDATE, DELETE, ALTER, REFERENCES ON OBJECT::[audit_2].[AgentAuditLog] TO [USR_FDE_AUDIT_R_2];

-- Neither audit login has any business in the data schemas.
DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[dbo] TO [USR_FDE_AUDIT_W_2], [USR_FDE_AUDIT_R_2];
DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[ai_2]  TO [USR_FDE_AUDIT_W_2], [USR_FDE_AUDIT_R_2];
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION, CREATE SCHEMA TO [USR_FDE_AUDIT_W_2], [USR_FDE_AUDIT_R_2];

-- The agent can neither read nor edit its own trail.
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_RO_2')
    DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[audit_2] TO [USR_FDE_RO_2];
GO
