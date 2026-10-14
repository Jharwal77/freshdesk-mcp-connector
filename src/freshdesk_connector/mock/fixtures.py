"""Fictional Freshdesk fixture data.

All data here is invented. Emails use ``example.com`` / ``example.in`` domains and
phone numbers are non-routable placeholders, so nothing can collide with a real
person. The subject matter is a D2C merchant using Razorpay - payment failures,
autopay mandates, settlements, refunds - because that is the ticket mix a
forward-deployed engineer actually meets.

Timestamps are generated relative to "now" at import time so SLA-breach and
recency logic behaves sensibly whenever the demo is run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# --------------------------------------------------------------------------- #
# People and orgs
# --------------------------------------------------------------------------- #

AGENTS = [
    {"id": 2001, "name": "Priya Nair", "email": "priya.nair@example.com", "groups": [9001, 9003]},
    {"id": 2002, "name": "Rohit Verma", "email": "rohit.verma@example.com", "groups": [9001]},
    {"id": 2003, "name": "Aisha Khan", "email": "aisha.khan@example.com", "groups": [9002]},
    {"id": 2004, "name": "Daniel Fernandes", "email": "daniel.fernandes@example.com", "groups": [9002, 9003]},
]

GROUPS = [
    {"id": 9001, "name": "Payments Support", "description": "UPI, cards, netbanking failures"},
    {"id": 9002, "name": "Settlements & Reconciliation", "description": "Payouts, settlement cycles, refunds"},
    {"id": 9003, "name": "Escalations", "description": "SLA-risk tickets and escalations"},
]

COMPANIES = [
    {"id": 5001, "name": "Vayu Wellness Pvt Ltd", "domains": ["vayuwellness.example"], "health_score": "Happy", "account_tier": "Growth", "industry": "Health & Wellness"},
    {"id": 5002, "name": "Kettle & Co", "domains": ["kettleco.example"], "health_score": "At Risk", "account_tier": "Enterprise", "industry": "Food & Beverage"},
    {"id": 5003, "name": "Bloom Studio", "domains": ["bloomstudio.example"], "health_score": "Happy", "account_tier": "Pro", "industry": "Retail"},
]

# (id, name, email, phone, mobile, company_id, job_title)
CONTACT_SEEDS = [
    (3001, "Ananya Sharma", "ananya.sharma@example.com", "+91 90000 11111", "9000011111", 5001, "Head of Operations"),
    (3002, "Vikram Rao", "vikram.rao@example.com", "+91 90000 22222", "9000022222", 5001, "Finance Manager"),
    (3003, "Meera Iyer", "meera.iyer@example.com", None, "9000033333", 5001, "Customer Experience Lead"),
    (3004, "Arjun Mehta", "arjun.mehta@example.com", "+91 90000 44444", "9000044444", 5002, "CTO"),
    (3005, "Sneha Kulkarni", "sneha.kulkarni@example.com", None, "9000055555", 5002, "Ops Analyst"),
    (3006, "Imran Sheikh", "imran.sheikh@example.com", "+91 90000 66666", "9000066666", 5002, "Store Manager"),
    (3007, "Divya Menon", "divya.menon@example.com", "+91 90000 77777", "9000077777", 5003, "Founder"),
    (3008, "Karthik Subramanian", "karthik.s@example.com", None, "9000088888", 5003, "Growth Lead"),
    (3009, "Ritu Bansal", "ritu.bansal@example.com", "+91 90000 99999", "9000099999", 5001, "Accounts Payable"),
    (3010, "Sameer Joshi", "sameer.joshi@example.com", None, "9000010101", 5003, "Support Engineer"),
]

# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
# Each seed: (subject, description, status, priority, source, type, contact_id,
#             agent_id, group_id, created_days_ago, updated_hours_ago,
#             due_in_hours, tags, custom_fields, escalated)
# status: 2 open, 3 pending, 4 resolved, 5 closed, 6 waiting on customer, 7 waiting on third party
# priority: 1 low, 2 medium, 3 high, 4 urgent
TICKET_SEEDS: list[dict[str, Any]] = [
    dict(
        subject="Autopay mandate failed but customer was still charged",
        description=(
            "Customer says the UPI autopay mandate debit failed on 03 Oct, yet the amount of Rs 1,499 "
            "was deducted twice. Requesting an immediate reconciliation and refund of the duplicate "
            "debit. Order id in custom fields."
        ),
        status=2, priority=4, source=1, type="Incident", contact=3001, agent=2001, group=9001,
        created_days_ago=1, updated_hours_ago=2, due_in_hours=-6,
        tags=["autopay", "duplicate-charge", "upi"], custom={"cf_order_id": "order_Nk82Hs91Lm", "cf_payment_id": "pay_Nk82Hs91Lm01", "cf_autopay_status": "failed"}, escalated=True,
    ),
    dict(
        subject="Refund of Rs 4,999 not received after 9 days",
        description="Refund was initiated on 28 Sep (ARN shared) but has not reflected in the customer's bank account. Customer is threatening a chargeback.",
        status=2, priority=4, source=1, type="Incident", contact=3004, agent=2004, group=9002,
        created_days_ago=2, updated_hours_ago=5, due_in_hours=-2,
        tags=["refund", "chargeback-risk"], custom={"cf_order_id": "order_Rf4491Kk2Z", "cf_payment_id": "pay_Rf4491Kk2Z", "cf_autopay_status": None}, escalated=True,
    ),
    dict(
        subject="Settlement for 30 Sep cycle is short by Rs 12,400",
        description="Merchant finance team reports the settlement batch for 30 Sep is short. Need a reconciliation breakdown by payment id.",
        status=2, priority=3, source=10, type="Problem", contact=3002, agent=2002, group=9002,
        created_days_ago=3, updated_hours_ago=26, due_in_hours=13,
        tags=["settlement", "reconciliation"], custom={"cf_order_id": None, "cf_payment_id": None, "cf_autopay_status": None}, escalated=False,
    ),
    dict(
        subject="How do I enable autopay for subscription renewals?",
        description="Operations team wants customers to opt into UPI autopay at checkout for the monthly subscription plan.",
        status=3, priority=2, source=2, type="Question", contact=3003, agent=2003, group=9001,
        created_days_ago=5, updated_hours_ago=50, due_in_hours=30,
        tags=["autopay", "subscription", "how-to"], custom={"cf_plan": "monthly"}, escalated=False,
    ),
    dict(
        subject="UPI payment stuck in pending for 45 minutes",
        description="Customer's UPI payment shows pending on the checkout page and the order was not confirmed. Payment id attached.",
        status=2, priority=3, source=7, type="Incident", contact=3007, agent=2001, group=9001,
        created_days_ago=0, updated_hours_ago=1, due_in_hours=4,
        tags=["upi", "pending-payment"], custom={"cf_order_id": "order_Up7781Qq4T", "cf_payment_id": "pay_Up7781Qq4T", "cf_autopay_status": None}, escalated=False,
    ),
    dict(
        subject="EMI options not showing at checkout for HDFC cards",
        description="Since yesterday the credit card EMI widget shows no plans for HDFC cards above Rs 10,000.",
        status=2, priority=3, source=1, type="Incident", contact=3008, agent=2002, group=9001,
        created_days_ago=1, updated_hours_ago=8, due_in_hours=6,
        tags=["emi", "cards"], custom={"cf_order_id": None, "cf_payment_id": None}, escalated=False,
    ),
    dict(
        subject="Webhook not firing for payment.captured events",
        description="Our order service stopped receiving payment.captured webhooks since 01 Oct. Signature verification is already implemented.",
        status=2, priority=4, source=1, type="Incident", contact=3004, agent=2004, group=9001,
        created_days_ago=2, updated_hours_ago=4, due_in_hours=-1,
        tags=["webhooks", "integration"], custom={"cf_order_id": None, "cf_payment_id": None}, escalated=True,
    ),
    dict(
        subject="KYC documents rejected without reason",
        description="Onboarding KYC was rejected but no reason code is visible. Merchant needs to go live this week.",
        status=6, priority=3, source=1, type="Problem", contact=3007, agent=2003, group=9003,
        created_days_ago=6, updated_hours_ago=72, due_in_hours=48,
        tags=["kyc", "onboarding"], custom={"cf_autopay_status": None}, escalated=False,
    ),
    dict(
        subject="Customer wants to cancel autopay mandate immediately",
        description="Customer emailed asking to stop the recurring debit. Please confirm the mandate has been revoked on our side too.",
        status=3, priority=3, source=1, type="Question", contact=3001, agent=2001, group=9001,
        created_days_ago=4, updated_hours_ago=30, due_in_hours=20,
        tags=["autopay", "cancellation"], custom={"cf_autopay_status": "active"}, escalated=False,
    ),
    dict(
        subject="COD remittance for last week not credited",
        description="Remittance for COD orders delivered between 22-28 Sep has not been credited to the bank account.",
        status=2, priority=2, source=1, type="Problem", contact=3005, agent=2002, group=9002,
        created_days_ago=7, updated_hours_ago=20, due_in_hours=36,
        tags=["cod", "remittance"], custom={}, escalated=False,
    ),
    dict(
        subject="Duplicate invoice generated for order ORD-99120",
        description="Two invoices were generated for a single order. Need one cancelled and the GST entry corrected.",
        status=3, priority=2, source=1, type="Problem", contact=3009, agent=2003, group=9002,
        created_days_ago=8, updated_hours_ago=40, due_in_hours=40,
        tags=["invoice", "gst"], custom={"cf_order_id": "order_Inv99120Aa"}, escalated=False,
    ),
    dict(
        subject="Payment link expired before customer could pay",
        description="The payment link we shared had a 24-hour expiry but the customer attempted within 12 hours and saw an expired page.",
        status=2, priority=2, source=1, type="Incident", contact=3006, agent=2001, group=9001,
        created_days_ago=3, updated_hours_ago=15, due_in_hours=18,
        tags=["payment-links"], custom={"cf_payment_id": "pay_Link5522Bb"}, escalated=False,
    ),
    dict(
        subject="International card declined with no clear error code",
        description="US-issued card attempts are failing. We need the exact decline reason to tell the customer what to fix.",
        status=2, priority=3, source=1, type="Question", contact=3008, agent=2002, group=9001,
        created_days_ago=9, updated_hours_ago=100, due_in_hours=24,
        tags=["international", "declines"], custom={}, escalated=False,
    ),
    dict(
        subject="Refund is processing for 12 days, customer disputing",
        description="Refund stuck in processing. Bank says no record. Customer has filed a dispute with their bank.",
        status=2, priority=4, source=1, type="Incident", contact=3005, agent=2004, group=9002,
        created_days_ago=11, updated_hours_ago=6, due_in_hours=-12,
        tags=["refund", "dispute"], custom={"cf_order_id": "order_Rf8812Zz", "cf_payment_id": "pay_Rf8812Zz"}, escalated=True,
    ),
    dict(
        subject="Autopay mandate created but not debited on due date",
        description="Mandate shows active with a Rs 999 monthly limit, but the 05 Oct debit never happened. Customer churned as a result.",
        status=2, priority=4, source=1, type="Incident", contact=3001, agent=2001, group=9001,
        created_days_ago=1, updated_hours_ago=3, due_in_hours=-4,
        tags=["autopay", "churn-risk", "mandate"], custom={"cf_autopay_status": "active", "cf_plan": "monthly"}, escalated=True,
    ),
    dict(
        subject="Need GST-compliant invoice format for bulk orders",
        description="Finance needs a modified invoice template with HSN codes for B2B orders above Rs 50,000.",
        status=3, priority=1, source=1, type="Feature Request", contact=3009, agent=2003, group=9002,
        created_days_ago=13, updated_hours_ago=120, due_in_hours=96,
        tags=["invoice", "feature-request"], custom={}, escalated=False,
    ),
    dict(
        subject="Dashboard shows zero successful payments for yesterday",
        description="The analytics dashboard shows zero successful payments for 12 Oct although the order system recorded 340 paid orders.",
        status=2, priority=3, source=1, type="Problem", contact=3004, agent=2004, group=9003,
        created_days_ago=1, updated_hours_ago=9, due_in_hours=8,
        tags=["dashboard", "reporting"], custom={}, escalated=False,
    ),
    dict(
        subject="Customer charged in USD instead of INR",
        description="A domestic customer was charged in USD with a foreign transaction fee. Needs a correction and refund of the FX markup.",
        status=2, priority=3, source=6, type="Incident", contact=3002, agent=2001, group=9001,
        created_days_ago=4, updated_hours_ago=12, due_in_hours=14,
        tags=["currency", "refund"], custom={"cf_payment_id": "pay_Fx7731Cc"}, escalated=False,
    ),
    dict(
        subject="How to test autopay flow in test mode?",
        description="Developer asks for guidance on simulating mandate failure and success in test mode before the production rollout.",
        status=4, priority=1, source=1, type="Question", contact=3008, agent=2003, group=9001,
        created_days_ago=15, updated_hours_ago=200, due_in_hours=None,
        tags=["autopay", "test-mode", "how-to"], custom={}, escalated=False,
    ),
    dict(
        subject="Chargeback raised on order ORD-88231",
        description="Customer's bank raised a chargeback. Merchant needs to submit evidence within the dispute window.",
        status=2, priority=4, source=1, type="Problem", contact=3007, agent=2004, group=9002,
        created_days_ago=5, updated_hours_ago=18, due_in_hours=-8,
        tags=["chargeback", "dispute", "evidence"], custom={"cf_order_id": "order_88231Cb", "cf_payment_id": "pay_88231Cb"}, escalated=True,
    ),
    dict(
        subject="Payout account change request pending approval",
        description="Merchant submitted a bank account change request 6 days ago; status still shows pending.",
        status=6, priority=3, source=1, type="Problem", contact=3002, agent=2002, group=9002,
        created_days_ago=6, updated_hours_ago=60, due_in_hours=12,
        tags=["payout", "bank-change"], custom={}, escalated=False,
    ),
    dict(
        subject="Mobile app checkout crashes on payment screen",
        description="Android app version 4.2 crashes when opening the payment screen. Crashlytics shows a null intent exception.",
        status=3, priority=4, source=1, type="Incident", contact=3004, agent=2004, group=9001,
        created_days_ago=2, updated_hours_ago=28, due_in_hours=2,
        tags=["mobile-sdk", "crash"], custom={}, escalated=True,
    ),
    dict(
        subject="Need sandbox credentials for the new integration",
        description="New developer joined and needs sandbox keys plus webhook signing secret for the staging environment.",
        status=5, priority=1, source=1, type="Question", contact=3010, agent=2003, group=9001,
        created_days_ago=20, updated_hours_ago=300, due_in_hours=None,
        tags=["sandbox", "credentials"], custom={}, escalated=False,
    ),
    dict(
        subject="Customer not receiving payment confirmation email",
        description="Order confirmations are not reaching customers on Outlook domains. SPF and DKIM entries were verified.",
        status=7, priority=2, source=1, type="Problem", contact=3003, agent=2003, group=9001,
        created_days_ago=10, updated_hours_ago=48, due_in_hours=42,
        tags=["email", "notifications"], custom={}, escalated=False,
    ),
    dict(
        subject="Partial refund on a split payment order",
        description="Order was paid by two payment methods; merchant needs to refund only the card portion of Rs 2,000.",
        status=2, priority=2, source=1, type="Question", contact=3006, agent=2001, group=9002,
        created_days_ago=14, updated_hours_ago=90, due_in_hours=52,
        tags=["partial-refund", "split-payment"], custom={"cf_order_id": "order_Split7788"}, escalated=False,
    ),
    dict(
        subject="Settlement cycle change to T+1 requested",
        description="Enterprise account wants T+1 settlement instead of T+2. Needs a risk and eligibility review.",
        status=3, priority=2, source=1, type="Feature Request", contact=3004, agent=2002, group=9002,
        created_days_ago=18, updated_hours_ago=150, due_in_hours=72,
        tags=["settlement", "enterprise"], custom={}, escalated=False,
    ),
    # --- older than Freshdesk's 30-day default listing window (created_days_ago > 30)
    dict(
        subject="Autopay mandate registration failing for new customers",
        description="Mandate registration via UPI returns a failure for customers added after 01 Sep. Older customers are unaffected.",
        status=2, priority=4, source=1, type="Incident", contact=3001, agent=2001, group=9001,
        created_days_ago=34, updated_hours_ago=20, due_in_hours=-20,
        tags=["autopay", "mandate", "regression"], custom={"cf_autopay_status": "failed"}, escalated=True,
    ),
    dict(
        subject="Settlement report missing UTR numbers",
        description="The settlement report downloaded from the dashboard no longer includes UTR numbers, blocking reconciliation.",
        status=4, priority=2, source=1, type="Problem", contact=3002, agent=2002, group=9002,
        created_days_ago=41, updated_hours_ago=260, due_in_hours=None,
        tags=["settlement", "reporting", "utr"], custom={}, escalated=False,
    ),
    dict(
        subject="Refund initiated twice for the same order",
        description="Two refunds of Rs 1,299 were processed for order ORD-77231. One needs to be reversed.",
        status=5, priority=3, source=1, type="Incident", contact=3005, agent=2004, group=9002,
        created_days_ago=47, updated_hours_ago=400, due_in_hours=None,
        tags=["refund", "duplicate-charge"], custom={"cf_order_id": "order_77231Dup"}, escalated=False,
    ),
    dict(
        subject="Need help reconciling autopay debits with our ERP",
        description="Finance wants a daily export of autopay debits to match against their ERP ledger.",
        status=3, priority=2, source=1, type="Feature Request", contact=3009, agent=2003, group=9002,
        created_days_ago=55, updated_hours_ago=500, due_in_hours=None,
        tags=["autopay", "reconciliation", "erp"], custom={}, escalated=False,
    ),
    dict(
        subject="Payment failure rate spiked on 22 Sep",
        description="Success rate dropped from 94% to 71% for two hours. Need a root cause analysis for the incident report.",
        status=4, priority=3, source=1, type="Problem", contact=3004, agent=2004, group=9003,
        created_days_ago=36, updated_hours_ago=180, due_in_hours=None,
        tags=["outage", "success-rate"], custom={}, escalated=False,
    ),
    # --- additional recent volume
    dict(
        subject="Autopay mandate limit change not reflecting",
        description="Customer requested a limit increase from Rs 999 to Rs 1,999 four days ago; the mandate still shows the old limit.",
        status=2, priority=3, source=1, type="Problem", contact=3003, agent=2001, group=9001,
        created_days_ago=4, updated_hours_ago=14, due_in_hours=10,
        tags=["autopay", "mandate", "limits"], custom={"cf_autopay_status": "active"}, escalated=False,
    ),
    dict(
        subject="Refund failed with error code REF-402",
        description="Bulk refund batch of 18 refunds failed with REF-402. Need to know which ones succeeded.",
        status=2, priority=4, source=1, type="Incident", contact=3009, agent=2004, group=9002,
        created_days_ago=0, updated_hours_ago=1, due_in_hours=3,
        tags=["refund", "batch", "failure"], custom={}, escalated=True,
    ),
    dict(
        subject="UPI collect request timing out for repeat customers",
        description="Collect requests time out after 90 seconds for customers who already have a mandate.",
        status=2, priority=3, source=1, type="Incident", contact=3007, agent=2002, group=9001,
        created_days_ago=2, updated_hours_ago=11, due_in_hours=5,
        tags=["upi", "collect-request", "timeout"], custom={}, escalated=False,
    ),
    dict(
        subject="Customer refunded but order still shows as paid",
        description="Refund succeeded but the order status in our system remains paid, causing a shipment to be dispatched.",
        status=2, priority=3, source=1, type="Problem", contact=3006, agent=2001, group=9002,
        created_days_ago=3, updated_hours_ago=16, due_in_hours=12,
        tags=["refund", "order-status", "webhooks"], custom={"cf_order_id": "order_Stale99"}, escalated=False,
    ),
    dict(
        subject="Autopay debit date changed without consent",
        description="Three customers reported their debit date moved from the 5th to the 12th without any request.",
        status=2, priority=4, source=1, type="Incident", contact=3001, agent=2004, group=9003,
        created_days_ago=1, updated_hours_ago=7, due_in_hours=-3,
        tags=["autopay", "mandate", "consent"], custom={"cf_autopay_status": "active"}, escalated=True,
    ),
    dict(
        subject="Bulk payout stuck in queued state",
        description="A payout batch of Rs 4.2L has been queued for six hours without moving to processing.",
        status=2, priority=4, source=1, type="Incident", contact=3004, agent=2002, group=9002,
        created_days_ago=0, updated_hours_ago=3, due_in_hours=2,
        tags=["payout", "queued", "batch"], custom={}, escalated=True,
    ),
    dict(
        subject="GST invoice missing for settlement month",
        description="September GST invoice was never generated, but the invoice for August is available.",
        status=3, priority=2, source=1, type="Problem", contact=3009, agent=2003, group=9002,
        created_days_ago=9, updated_hours_ago=55, due_in_hours=30,
        tags=["invoice", "gst", "settlement"], custom={}, escalated=False,
    ),
    dict(
        subject="Autopay retry logic questions before go-live",
        description="Team wants to confirm how many retries happen after a failed autopay debit and at what intervals.",
        status=3, priority=2, source=2, type="Question", contact=3008, agent=2003, group=9001,
        created_days_ago=6, updated_hours_ago=44, due_in_hours=26,
        tags=["autopay", "how-to", "retries"], custom={}, escalated=False,
    ),
    dict(
        subject="Chargeback evidence upload failing",
        description="Uploading evidence for a chargeback returns a 500 error on the disputes page.",
        status=2, priority=3, source=1, type="Incident", contact=3007, agent=2004, group=9002,
        created_days_ago=2, updated_hours_ago=13, due_in_hours=9,
        tags=["chargeback", "dispute", "evidence"], custom={}, escalated=False,
    ),
    dict(
        subject="Customer disputes autopay debit as unauthorised",
        description="Customer claims they never signed up for autopay. Need the mandate registration trail for the bank.",
        status=2, priority=4, source=5, type="Incident", contact=3001, agent=2001, group=9003,
        created_days_ago=1, updated_hours_ago=9, due_in_hours=-5,
        tags=["autopay", "dispute", "unauthorised"], custom={"cf_autopay_status": "active"}, escalated=True,
    ),
    dict(
        subject="Refund TAT reported as 5-7 days, bank says 10",
        description="Customer support scripts promise 5-7 day refunds; the bank is quoting 10 working days, causing escalations.",
        status=6, priority=2, source=1, type="Question", contact=3003, agent=2003, group=9002,
        created_days_ago=12, updated_hours_ago=80, due_in_hours=40,
        tags=["refund", "tat", "support-scripts"], custom={}, escalated=False,
    ),
    dict(
        subject="Autopay mandate revoked but debits continue",
        description="Customer revoked the mandate on 28 Sep yet two further debits went through. Urgent reversal needed.",
        status=2, priority=4, source=1, type="Incident", contact=3002, agent=2001, group=9001,
        created_days_ago=3, updated_hours_ago=5, due_in_hours=-7,
        tags=["autopay", "mandate", "duplicate-charge", "urgent"], custom={"cf_autopay_status": "revoked"}, escalated=True,
    ),
    dict(
        subject="Settlement split across two UTRs needs explanation",
        description="Thursday's settlement arrived as two UTRs. Finance needs a breakdown of which orders belong to each.",
        status=3, priority=2, source=1, type="Question", contact=3005, agent=2002, group=9002,
        created_days_ago=8, updated_hours_ago=70, due_in_hours=34,
        tags=["settlement", "utr", "reconciliation"], custom={}, escalated=False,
    ),
    dict(
        subject="Webhook retries exhausted for 340 events",
        description="Our endpoint was down for maintenance and 340 webhook deliveries hit the retry ceiling. Can they be replayed?",
        status=2, priority=3, source=1, type="Problem", contact=3004, agent=2002, group=9001,
        created_days_ago=5, updated_hours_ago=22, due_in_hours=11,
        tags=["webhooks", "replay", "integration"], custom={}, escalated=False,
    ),
]

# Custom fields exposed by this account's ticket_fields endpoint.
CUSTOM_TICKET_FIELDS = [
    {"name": "cf_order_id", "label": "Razorpay Order ID", "type": "custom_text"},
    {"name": "cf_payment_id", "label": "Razorpay Payment ID", "type": "custom_text"},
    {"name": "cf_plan", "label": "Subscription Plan", "type": "custom_dropdown", "choices": {"monthly": "Monthly", "annual": "Annual"}},
    {"name": "cf_autopay_status", "label": "Autopay Mandate Status", "type": "custom_dropdown", "choices": {"active": "Active", "failed": "Failed", "revoked": "Revoked"}},
]

STANDARD_TICKET_FIELDS = [
    {"name": "status", "label": "Status", "type": "default_status", "default": True, "choices": {"2": "Open", "3": "Pending", "4": "Resolved", "5": "Closed", "6": "Waiting on Customer", "7": "Waiting on Third Party"}},
    {"name": "priority", "label": "Priority", "type": "default_priority", "default": True, "choices": {"1": "Low", "2": "Medium", "3": "High", "4": "Urgent"}},
    {"name": "source", "label": "Source", "type": "default_source", "default": True},
    {"name": "type", "label": "Ticket Type", "type": "default_ticket_type", "default": True},
    {"name": "group_id", "label": "Group", "type": "default_group", "default": True},
    {"name": "agent_id", "label": "Agent", "type": "default_agent", "default": True},
    {"name": "requester_id", "label": "Requester", "type": "default_requester", "default": True},
    {"name": "company_id", "label": "Company", "type": "default_company", "default": True},
    {"name": "created_at", "label": "Created", "type": "default_created_at", "default": True},
    {"name": "updated_at", "label": "Updated", "type": "default_updated_at", "default": True},
    {"name": "due_by", "label": "Due by", "type": "default_due_by", "default": True},
    {"name": "fr_due_by", "label": "First response due by", "type": "default_fr_due_by", "default": True},
    {"name": "tag", "label": "Tags", "type": "default_tags", "default": True},
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_contacts() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out = []
    for idx, (cid, name, email, phone, mobile, company_id, title) in enumerate(CONTACT_SEEDS):
        out.append(
            {
                "id": cid,
                "name": name,
                "email": email,
                "phone": phone,
                "mobile": mobile,
                "job_title": title,
                "company_id": company_id,
                "other_company_ids": [],
                "tags": [],
                "created_at": _iso(now - timedelta(days=120 - idx)),
                "updated_at": _iso(now - timedelta(days=10)),
            }
        )
    return out


def build_tickets() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    tickets: list[dict[str, Any]] = []
    for i, seed in enumerate(TICKET_SEEDS):
        tid = 10001 + i
        created = now - timedelta(days=seed["created_days_ago"], hours=(i * 3) % 20)
        updated = now - timedelta(hours=seed["updated_hours_ago"])
        if updated < created:
            updated = created + timedelta(hours=1)
        contact = next(c for c in CONTACT_SEEDS if c[0] == seed["contact"])
        due = (
            now + timedelta(hours=seed["due_in_hours"])
            if seed.get("due_in_hours") is not None
            else None
        )
        fr_due = now + timedelta(hours=(seed.get("due_in_hours") or 24) / 2)

        resolved_at = updated if seed["status"] in (4, 5) else None
        stats: dict[str, Any] = {
            "created_at": _iso(created),
            "updated_at": _iso(updated),
            "first_responded_at": _iso(created + timedelta(hours=2)),
            "requester_responded_at": _iso(updated) if seed["status"] in (3, 6) else None,
            "pending_since": _iso(updated) if seed["status"] == 3 else None,
            "resolved_at": _iso(resolved_at) if resolved_at else None,
            "closed_at": _iso(updated) if seed["status"] == 5 else None,
        }

        ticket: dict[str, Any] = {
            "id": tid,
            "subject": seed["subject"],
            "description": f"<div>{seed['description']}</div>",
            "description_text": seed["description"],
            "status": seed["status"],
            "priority": seed["priority"],
            "source": seed["source"],
            "type": seed["type"],
            "requester_id": seed["contact"],
            "responder_id": seed["agent"],
            "group_id": seed["group"],
            "company_id": contact[5],
            "product_id": None,
            "tags": list(seed["tags"]),
            "created_at": _iso(created),
            "updated_at": _iso(updated),
            "due_by": _iso(due) if due else None,
            "fr_due_by": _iso(fr_due),
            "fr_escalated": bool(seed.get("escalated")),
            "is_escalated": bool(seed.get("escalated")),
            "spam": False,
            "deleted": False,
            "stats": stats,
            "email_config_id": None,
            "canned_response_ids": [],
            "custom_fields": seed.get("custom", {}),
        }
        # Freshdesk surfaces custom fields as top-level `cf_*` keys on the object.
        for key, value in (seed.get("custom") or {}).items():
            ticket[key] = value
        tickets.append(ticket)
    return tickets


def build_conversations() -> dict[int, list[dict[str, Any]]]:
    now = datetime.now(timezone.utc)
    out: dict[int, list[dict[str, Any]]] = {}
    for i, seed in enumerate(TICKET_SEEDS):
        tid = 10001 + i
        created = now - timedelta(days=seed["created_days_ago"], hours=(i * 3) % 20)
        convs = [
            {
                "id": tid * 10 + 1,
                "ticket_id": tid,
                "user_id": seed["contact"],
                "private": False,
                "incoming": True,
                "body": f"<div>{seed['description']}</div>",
                "body_text": seed["description"],
                "attachments": [],
                "created_at": _iso(created),
                "updated_at": _iso(created),
                "source": 1,
            },
            {
                "id": tid * 10 + 2,
                "ticket_id": tid,
                "user_id": seed["agent"],
                "private": True,
                "incoming": False,
                "body": "<div>Internal note: checking with risk team; do not share externally.</div>",
                "body_text": "Internal note: checking with risk team; do not share externally.",
                "attachments": [],
                "created_at": _iso(created + timedelta(hours=1)),
                "updated_at": _iso(created + timedelta(hours=1)),
                "source": 0,
            },
        ]
        if seed["status"] in (2, 3, 6, 7):
            replied = created + timedelta(hours=3)
            convs.append(
                {
                    "id": tid * 10 + 3,
                    "ticket_id": tid,
                    "user_id": seed["agent"],
                    "private": False,
                    "incoming": False,
                    "body": "<div>Thanks for reaching out - we are investigating and will update you shortly.</div>",
                    "body_text": "Thanks for reaching out - we are investigating and will update you shortly.",
                    "attachments": [],
                    "created_at": _iso(replied),
                    "updated_at": _iso(replied),
                    "source": 0,
                }
            )
        out[tid] = convs
    return out


def build_agents() -> list[dict[str, Any]]:
    return [
        {
            "id": a["id"],
            "available": True,
            "occasional": False,
            "group_ids": a["groups"],
            "ticket_scope": 1,
            "contact": {"id": a["id"] + 100000, "name": a["name"], "email": a["email"], "active": True},
        }
        for a in AGENTS
    ]


def build_groups() -> list[dict[str, Any]]:
    return [
        {
            "id": g["id"],
            "name": g["name"],
            "description": g["description"],
            "agent_ids": sorted({a["id"] for a in AGENTS if g["id"] in a["groups"]}),
            "escalate_to": 2004 if g["id"] == 9001 else None,
            "business_hour_id": 1,
        }
        for g in GROUPS
    ]


def build_companies() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "domains": c["domains"],
            "health_score": c["health_score"],
            "account_tier": c["account_tier"],
            "industry": c["industry"],
            "created_at": _iso(now - timedelta(days=300)),
        }
        for c in COMPANIES
    ]


def build_ticket_fields() -> list[dict[str, Any]]:
    fields = [dict(f) for f in STANDARD_TICKET_FIELDS]
    for f in CUSTOM_TICKET_FIELDS:
        entry = dict(f)
        entry["default"] = False
        fields.append(entry)
    return fields
