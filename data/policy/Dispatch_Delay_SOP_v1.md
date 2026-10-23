# Southern California Logistics Operations
## Standard Operating Procedure (SOP): Dispatch, Delays & Rerouting
**Version:** 1.0 | **Effective Date:** Jan 2021 | **Confidentiality:** Internal Operations Only
*Sample policy for demonstration. Thresholds are illustrative.*

### 1. Delay Escalation
Dispatchers track the gap between estimated and actual arrival (`ETA_VAR_HR`, in hours) and the delivery time deviation (`DEL_TM_DEV`, in hours).
* **Customer Notice:** If `ETA_VAR_HR` exceeds **3.0 hours**, notify the customer within 30 minutes.
* **Tier 2 Escalation:** If `ETA_VAR_HR` exceeds **4.5 hours**, or `DEL_TM_DEV` exceeds **8.0 hours**, escalate to the Tier 2 Logistics Manager.

### 2. Rerouting on Road Congestion
Road congestion is measured by `TRF_CONG_LVL` on a 0 to 10 scale.
* **Trigger:** If `TRF_CONG_LVL` is **8.0 or higher** and `DELAY_PROB` is greater than **0.7**, the dispatcher must offer the driver an alternate arterial route.
* **Mitigation Protocol:** Record the reroute decision and the new estimated arrival time in the dispatch log.

### 3. Severe Weather Holds
Weather severity is measured by `WX_SEV` on a 0 to 1 scale.
* **Departure Hold:** If `WX_SEV` exceeds **0.8** at the departure point, hold new departures until the dispatcher confirms the route is safe.
* **Live Check:** Always check live weather at the departure location before releasing a held vehicle.
