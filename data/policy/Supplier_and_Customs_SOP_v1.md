# Southern California Logistics Operations
## Standard Operating Procedure (SOP): Supplier Reliability & Customs
**Version:** 1.0 | **Effective Date:** Jan 2021 | **Confidentiality:** Internal Operations Only
*Sample policy for demonstration. Thresholds are illustrative.*

### 1. Supplier Reliability
Supplier reliability (`SUPL_REL_SCR`) is a score from 0 (unreliable) to 1 (fully reliable).
* **Watch List:** If `SUPL_REL_SCR` falls below **0.3**, place the supplier on the watch list and require a named backup supplier.
* **Order Suspension:** If `SUPL_REL_SCR` falls below **0.15**, suspend new orders until Procurement approves a recovery plan.

### 2. Lead Time
Lead time is recorded in days (`LEAD_TM_DY`).
* **Expedite Review:** If `LEAD_TM_DY` exceeds **10 days**, Procurement must review whether to expedite or change supplier.

### 3. Customs Clearance
Customs clearance time is recorded in hours (`CUST_CLR_TM`).
* **Broker Escalation:** If `CUST_CLR_TM` exceeds **3.5 hours**, escalate to the customs broker.
* **Port Congestion:** If `PORT_CONG_LVL` exceeds **7.0**, follow the diversion rule in the Cold-Chain & Transit Anomalies SOP, section 2.
