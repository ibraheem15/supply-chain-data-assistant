-- ====================================================================
-- FDE ENTERPRISE SECURITY & SEMANTIC LAYER SCRIPT
-- Purpose: Protect the legacy DB from LLM hallucinations and mutations
--
-- Run with:  make agent-db-setup
-- sqlcmd variables (passed with -v, never hard-coded here):
--   DbName         target database, e.g. SupplyChainDb
--   AgentPassword  password for the USR_FDE_RO login (no single quotes)
--
-- Idempotent: safe to re-run. Re-running also rotates the login password.
-- Requires dbo.supply_chain_logistics to exist (run `make ingest` first).
-- ====================================================================

:on error exit

-- 0. Server-level login (logins live in master; users live in a database)
USE [master];
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'USR_FDE_RO')
    CREATE LOGIN [USR_FDE_RO] WITH PASSWORD = N'$(AgentPassword)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN [USR_FDE_RO] WITH PASSWORD = N'$(AgentPassword)';
GO

USE [$(DbName)];
GO

-- 1. Create a dedicated schema for our clean AI views
--    Owned by dbo so it shares an owner with dbo.supply_chain_logistics: that intact
--    "ownership chain" is what lets the user read the view without any permission on
--    the underlying table.
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'ai')
    EXEC (N'CREATE SCHEMA [ai] AUTHORIZATION [dbo]');
GO

-- 2. Create the Semantic View (Translating legacy junk to clean English)
--    Column aliases are the ubiquitous-language names (COLUMNS in
--    ingest_supply_chain_dataset.py). LEGACY_COLUMN_NAMES there and the left-hand
--    side below are the same mapping: keep them in sync.
--    The dataset has no shipment id or cargo type, so this is a view of hourly
--    readings, not an "active fleet".
CREATE OR ALTER VIEW [ai].[VW_SUPPLY_CHAIN_READINGS] AS
SELECT
    [TM_UTC]        AS [timestamp],
    [VEH_LAT]       AS [vehicle_gps_latitude],
    [VEH_LON]       AS [vehicle_gps_longitude],
    [FUEL_RT_LPH]   AS [fuel_consumption_rate],
    [ETA_VAR_HR]    AS [eta_variation_hours],
    [TRF_CONG_LVL]  AS [traffic_congestion_level],
    [WH_INV_LVL]    AS [warehouse_inventory_level],
    [LD_UNLD_TM]    AS [loading_unloading_time],
    [HDL_EQP_AVAIL] AS [handling_equipment_availability],
    [ORD_FULFIL_ST] AS [order_fulfillment_status],
    [WX_SEV]        AS [weather_condition_severity],
    [PORT_CONG_LVL] AS [port_congestion_level],
    [SHIP_COST_USD] AS [shipping_costs],
    [SUPL_REL_SCR]  AS [supplier_reliability_score],
    [LEAD_TM_DY]    AS [lead_time_days],
    [HIST_DMD]      AS [historical_demand],
    [IOT_TEMP_C]    AS [iot_temperature],
    [CGO_COND_ST]   AS [cargo_condition_status],
    [RT_RISK_LVL]   AS [route_risk_level],
    [CUST_CLR_TM]   AS [customs_clearance_time],
    [DRV_BHVR_SCR]  AS [driver_behavior_score],
    [FTG_MON_SCR]   AS [fatigue_monitoring_score],
    [DISR_LKHD_SCR] AS [disruption_likelihood_score],
    [DELAY_PROB]    AS [delay_probability],
    [RISK_CLASS]    AS [risk_classification],
    [DEL_TM_DEV]    AS [delivery_time_deviation]
FROM [dbo].[supply_chain_logistics];
GO

-- 3. Create a strict Read-Only Login and User for the AI Agent
--    The user is only a member of `public`: no db_datareader, no other roles.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_RO')
    CREATE USER [USR_FDE_RO] FOR LOGIN [USR_FDE_RO] WITH DEFAULT_SCHEMA = [ai];
GO

-- 4. Grant access ONLY to the semantic view, explicitly denying everything else
--    GRANT is on the single object, not the schema, so a future view in [ai] is
--    not exposed to the agent by accident.
GRANT SELECT ON OBJECT::[ai].[VW_SUPPLY_CHAIN_READINGS] TO [USR_FDE_RO];

--    DENY wins over any GRANT and over role membership. Belt and braces: the view
--    still works because ownership chaining skips permission checks on dbo.
DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER, VIEW DEFINITION ON SCHEMA::[dbo] TO [USR_FDE_RO];
DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[ai] TO [USR_FDE_RO];
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION, CREATE SCHEMA TO [USR_FDE_RO];
GO

-- 5. Self-check: act as the agent and prove the boundary holds.
--    Each line must print PASS; any FAIL aborts the script.
SET NOCOUNT ON;
DECLARE @failed BIT = 0;

EXECUTE AS USER = N'USR_FDE_RO';

BEGIN TRY
    DECLARE @rows BIGINT = (SELECT COUNT_BIG(*) FROM [ai].[VW_SUPPLY_CHAIN_READINGS]);
    PRINT CASE WHEN @rows > 0 THEN 'PASS  SELECT on view returns rows'
               ELSE 'FAIL  view is empty (run make ingest first)' END;
    IF @rows = 0 SET @failed = 1;
END TRY
BEGIN CATCH
    PRINT 'FAIL  SELECT on view: ' + ERROR_MESSAGE(); SET @failed = 1;
END CATCH;

BEGIN TRY
    EXEC (N'SELECT TOP 1 * FROM [dbo].[supply_chain_logistics]');
    PRINT 'FAIL  agent can read the raw table'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  raw table SELECT denied'; END CATCH;

BEGIN TRY
    EXEC (N'DELETE FROM [ai].[VW_SUPPLY_CHAIN_READINGS]');
    PRINT 'FAIL  agent can DELETE through the view'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  DELETE through view denied'; END CATCH;

BEGIN TRY
    EXEC (N'UPDATE [ai].[VW_SUPPLY_CHAIN_READINGS] SET [iot_temperature] = 0');
    PRINT 'FAIL  agent can UPDATE through the view'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  UPDATE through view denied'; END CATCH;

BEGIN TRY
    EXEC (N'DROP TABLE [dbo].[supply_chain_logistics]');
    PRINT 'FAIL  agent can DROP the table'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  DROP TABLE denied'; END CATCH;

BEGIN TRY
    EXEC (N'CREATE TABLE [ai].[agent_probe] (id INT)');
    PRINT 'FAIL  agent can CREATE objects'; SET @failed = 1;
END TRY
BEGIN CATCH PRINT 'PASS  CREATE TABLE denied'; END CATCH;

REVERT;

IF @failed = 1
    RAISERROR (N'Security self-check failed.', 16, 1);
ELSE
    PRINT 'All security checks passed.';
GO
