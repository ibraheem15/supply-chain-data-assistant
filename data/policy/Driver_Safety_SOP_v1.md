# Southern California Logistics Operations
## Standard Operating Procedure (SOP): Driver Safety & Fatigue
**Version:** 1.0 | **Effective Date:** Jan 2021 | **Confidentiality:** Internal Operations Only
*Sample policy for demonstration. Thresholds are illustrative. Company scoring convention: 1.0 is the best score for behaviour and the highest fatigue for the fatigue score.*

### 1. Driver Fatigue
Fatigue is monitored with a score from 0 (rested) to 1 (exhausted), recorded as `FTG_MON_SCR`.
* **Mandatory Rest:** If `FTG_MON_SCR` exceeds **0.8**, the driver must take a rest break of at least 30 minutes before the next leg.
* **Relief Driver:** If `FTG_MON_SCR` exceeds **0.9**, the dispatcher must arrange a relief driver.

### 2. Driver Behaviour
Driving behaviour is scored from 0 to 1, recorded as `DRV_BHVR_SCR`.
* **Coaching:** If `DRV_BHVR_SCR` falls below **0.2**, schedule a coaching session within 7 days.

### 3. Reporting Rules
Safety reviews report readings and patterns only. The monitoring data carries no driver identity, so reviews must not name, rank or blame an individual.
