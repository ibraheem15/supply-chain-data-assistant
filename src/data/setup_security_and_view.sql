-- Logins are stored in master db
-- Users are stored in specific databases, not in the master db.
-- Think of it as a building. The login is the keycard that gets you through the front door. The user is the permission list for one particular room. A card with no entry on a room's list gets you into the building but not into that room.
USE [master];
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'USR_FDE_RO')
    CREATE LOGIN [USR_FDE_RO] WITH PASSWORD = N'$(AgentPassword)';
ELSE
    ALTER LOGIN [USR_FDE_RO] WITH PASSWORD = N'$(AgentPassword)';
GO

-- A schema is a namespace inside a database, like a package or folder. Your table is in dbo, and we put the agent-facing objects in ai. 
-- A view is a saved SELECT that behaves like a table. The agent will query the view and never see the legacy column names. 

USE [SupplyChainDb];
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'ai')
    EXEC('CREATE SCHEMA [ai] AUTHORIZATION [dbo]');
GO
-- When a view and its table have the same owner (dbo), SQL Server skips the permission check on the table. So the agent can read the view without any permission on the table. The agent isn't the owner, and the schema's owner is the key to this. Step 3 depends on it.

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

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'USR_FDE_RO')
    CREATE USER [USR_FDE_RO] FOR LOGIN [USR_FDE_RO] WITH DEFAULT_SCHEMA = [ai];
GO
    
-- Grant the read-only user access to the view in the ai schema.
GRANT SELECT ON OBJECT::[ai].[VW_SUPPLY_CHAIN_READINGS] TO [USR_FDE_RO];
GO

-- DENY all other operations
DENY SELECT, INSERT, UPDATE, DELETE, EXECUTE, ALTER, VIEW DEFINITION ON SCHEMA::[dbo] TO [USR_FDE_RO];
DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER ON SCHEMA::[ai] TO [USR_FDE_RO];
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION, CREATE SCHEMA TO [USR_FDE_RO];
GO

-- Testing
-- Test if the read-only user can access the raw table in the dbo schema.
-- Expected result: access should be denied.

-- Switch to the read-only user context for testing.
EXECUTE AS USER = 'USR_FDE_RO';

SELECT TOP 1 * FROM [ai].[VW_SUPPLY_CHAIN_READINGS];

BEGIN TRY
    EXEC (N'SELECT TOP 1 * FROM [dbo].[supply_chain_logistics]');
    PRINT 'FAIL  agent can read the raw table';
END TRY
BEGIN CATCH
    PRINT 'PASS  raw table SELECT denied';
END CATCH;

-- Revert to the original user context after testing.
REVERT;
GO