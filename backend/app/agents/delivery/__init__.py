"""GPT-owned cross-discipline delivery-agent plugins."""

from app.agents.delivery.alarm_engineering import AlarmEngineeringAgent
from app.agents.delivery.electrical_io import ElectricalIOAgent
from app.agents.delivery.hmi_engineering import HMIEngineeringAgent
from app.agents.delivery.report_delivery import EngineeringReportDeliveryAgent
from app.agents.delivery.testing_commissioning import TestingCommissioningAgent

DELIVERY_AGENTS = (
    ElectricalIOAgent(),
    HMIEngineeringAgent(),
    AlarmEngineeringAgent(),
    TestingCommissioningAgent(),
    EngineeringReportDeliveryAgent(),
)

__all__ = [
    "DELIVERY_AGENTS",
    "AlarmEngineeringAgent",
    "ElectricalIOAgent",
    "EngineeringReportDeliveryAgent",
    "HMIEngineeringAgent",
    "TestingCommissioningAgent",
]
