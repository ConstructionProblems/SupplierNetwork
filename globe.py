"""
Streamlit prototype for visualizing a multi-tier supply chain network.

The application implements the "Supply Chain Mapping Prototype" specification.
Users can manage products, components, suppliers, facilities, and material
flows, then explore the network on an interactive map rendered with pydeck.
"""

from __future__ import annotations

import io
import json
import math
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd
import pydeck as pdk
import streamlit as st
from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker


# ------------------------------------------------------------------------------
# Database models
# ------------------------------------------------------------------------------

Base = declarative_base()


def uuid_str() -> str:
    return str(uuid.uuid4())


class Product(Base):
    __tablename__ = "products"

    id = Column(String(36), primary_key=True, default=uuid_str)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, default="")
    client_code = Column(String(64), nullable=True)
    lifecycle_status = Column(String(32), default="active")

    components = relationship("Component", back_populates="product", cascade="all, delete-orphan")


class Component(Base):
    __tablename__ = "components"

    id = Column(String(36), primary_key=True, default=uuid_str)
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    name = Column(String(255), nullable=False)
    spec_ref = Column(String(255), default="")
    uom = Column(String(32), default="")
    qty_per_product = Column(Float, default=1.0)
    notes = Column(Text, default="")

    product = relationship("Product", back_populates="components")
    supplier_links = relationship("SupplierComponent", back_populates="component", cascade="all, delete-orphan")
    facility_links = relationship("FacilityComponent", back_populates="component", cascade="all, delete-orphan")
    flows = relationship("MaterialFlow", back_populates="component")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(String(36), primary_key=True, default=uuid_str)
    name = Column(String(255), unique=True, nullable=False)
    tier = Column(Integer, default=1)
    address = Column(Text, default="")
    city = Column(String(120), default="")
    country = Column(String(120), default="")
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    primary_contact = Column(String(255), default="")
    email = Column(String(255), default="")
    phone = Column(String(64), default="")
    supply_node_id = Column(String(36), ForeignKey("supply_nodes.id"), nullable=True)

    supply_node = relationship("SupplyNode", back_populates="supplier")
    components = relationship("SupplierComponent", back_populates="supplier", cascade="all, delete-orphan")


class Facility(Base):
    __tablename__ = "facilities"

    id = Column(String(36), primary_key=True, default=uuid_str)
    name = Column(String(255), unique=True, nullable=False)
    facility_type = Column(String(32), nullable=False)  # assembly | sub_assembly
    address = Column(Text, default="")
    city = Column(String(120), default="")
    country = Column(String(120), default="")
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    operations = Column(Text, default="[]")
    supply_node_id = Column(String(36), ForeignKey("supply_nodes.id"), nullable=True)

    supply_node = relationship("SupplyNode", back_populates="facility")
    components = relationship("FacilityComponent", back_populates="facility", cascade="all, delete-orphan")

    def operations_list(self) -> List[str]:
        try:
            data = json.loads(self.operations or "[]")
            if isinstance(data, list):
                return [str(item) for item in data]
        except json.JSONDecodeError:
            return []
        return []

    def set_operations(self, entries: Sequence[str]) -> None:
        cleaned = [entry.strip() for entry in entries if entry.strip()]
        self.operations = json.dumps(cleaned)


class SupplyNode(Base):
    __tablename__ = "supply_nodes"

    id = Column(String(36), primary_key=True, default=uuid_str)
    node_type = Column(String(32), nullable=False)  # supplier | facility
    entity_id = Column(String(36), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    city = Column(String(120), default="")
    country = Column(String(120), default="")
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    tier = Column(Integer, nullable=True)

    supplier = relationship("Supplier", back_populates="supply_node", uselist=False)
    facility = relationship("Facility", back_populates="supply_node", uselist=False)
    outgoing_flows = relationship(
        "MaterialFlow",
        back_populates="from_node",
        foreign_keys="MaterialFlow.from_node_id",
        cascade="all, delete-orphan",
    )
    incoming_flows = relationship(
        "MaterialFlow",
        back_populates="to_node",
        foreign_keys="MaterialFlow.to_node_id",
    )


class MaterialFlow(Base):
    __tablename__ = "material_flows"

    id = Column(String(36), primary_key=True, default=uuid_str)
    from_node_id = Column(String(36), ForeignKey("supply_nodes.id"), nullable=False)
    to_node_id = Column(String(36), ForeignKey("supply_nodes.id"), nullable=False)
    component_id = Column(String(36), ForeignKey("components.id"), nullable=True)
    flow_type = Column(String(64), default="component")
    lead_time_days = Column(Float, nullable=True)
    incoterms = Column(String(32), default="")
    notes = Column(Text, default="")

    from_node = relationship("SupplyNode", foreign_keys=[from_node_id], back_populates="outgoing_flows")
    to_node = relationship("SupplyNode", foreign_keys=[to_node_id], back_populates="incoming_flows")
    component = relationship("Component", back_populates="flows")


class SupplierComponent(Base):
    __tablename__ = "supplier_components"

    supplier_id = Column(String(36), ForeignKey("suppliers.id"), primary_key=True)
    component_id = Column(String(36), ForeignKey("components.id"), primary_key=True)
    capacity_units_per_month = Column(Float, nullable=True)
    moq = Column(Float, nullable=True)
    lead_time_days = Column(Float, nullable=True)

    supplier = relationship("Supplier", back_populates="components")
    component = relationship("Component", back_populates="supplier_links")


class FacilityComponent(Base):
    __tablename__ = "facility_components"

    facility_id = Column(String(36), ForeignKey("facilities.id"), primary_key=True)
    component_id = Column(String(36), ForeignKey("components.id"), primary_key=True)
    operation = Column(String(128), default="")

    facility = relationship("Facility", back_populates="components")
    component = relationship("Component", back_populates="facility_links")


# ------------------------------------------------------------------------------
# Helper functions for supply nodes
# ------------------------------------------------------------------------------

def ensure_supply_node_for_supplier(session: Session, supplier: Supplier) -> SupplyNode:
    node = supplier.supply_node
    if node is None:
        node = SupplyNode(
            node_type="supplier",
            entity_id=supplier.id,
            name=supplier.name,
            city=supplier.city,
            country=supplier.country,
            latitude=supplier.latitude,
            longitude=supplier.longitude,
            tier=supplier.tier,
        )
        session.add(node)
        session.flush()
        supplier.supply_node = node
        supplier.supply_node_id = node.id
    else:
        node.name = supplier.name
        node.city = supplier.city
        node.country = supplier.country
        node.latitude = supplier.latitude
        node.longitude = supplier.longitude
        node.tier = supplier.tier
    return node


def ensure_supply_node_for_facility(session: Session, facility: Facility) -> SupplyNode:
    node = facility.supply_node
    if node is None:
        node = SupplyNode(
            node_type="facility",
            entity_id=facility.id,
            name=facility.name,
            city=facility.city,
            country=facility.country,
            latitude=facility.latitude,
            longitude=facility.longitude,
        )
        session.add(node)
        session.flush()
        facility.supply_node = node
        facility.supply_node_id = node.id
    else:
        node.name = facility.name
        node.city = facility.city
        node.country = facility.country
        node.latitude = facility.latitude
        node.longitude = facility.longitude
    return node


# ------------------------------------------------------------------------------
# Sample dataset (bogus values for the Maple Grid Systems transformer)
# ------------------------------------------------------------------------------

SAMPLE_PRODUCT = {
    "name": "MGS-TX500 250 MVA Power Transformer",
    "client_code": "MGS-TX500",
    "description": "Flagship 250 MVA high-voltage transformer for Maple Grid Systems.",
    "lifecycle_status": "active",
}

SAMPLE_COMPONENTS = [
    {"name": "Electrical steel laminations", "qty": 12000.0, "uom": "kg"},
    {"name": "Copper windings", "qty": 8500.0, "uom": "kg"},
    {"name": "Insulating oil", "qty": 18000.0, "uom": "L"},
    {"name": "Bushings HV", "qty": 3.0, "uom": "ea"},
    {"name": "On-load tap changer", "qty": 1.0, "uom": "ea"},
    {"name": "Tank & Radiator assembly", "qty": 1.0, "uom": "set"},
    {"name": "Core clamping hardware", "qty": 1.0, "uom": "set"},
    {"name": "Control cabinet & protection relays", "qty": 1.0, "uom": "set"},
]

SAMPLE_FACILITIES = [
    {
        "name": "Maple Grid Systems – Cambridge Final Assembly",
        "type": "assembly",
        "address": "1200 Franklin Blvd, Cambridge, ON, Canada",
        "city": "Cambridge",
        "country": "Canada",
        "latitude": 43.389,
        "longitude": -80.329,
        "operations": ["Final assembly", "Testing", "Quality assurance"],
    },
    {
        "name": "Maple Grid Systems – Windsor Coil Shop",
        "type": "sub_assembly",
        "address": "2550 Huron Church Rd, Windsor, ON, Canada",
        "city": "Windsor",
        "country": "Canada",
        "latitude": 42.262,
        "longitude": -83.039,
        "operations": ["Coil winding", "Core stacking"],
    },
]

SAMPLE_SUPPLIERS_TIER1 = [
    {
        "name": "Northern Core Metals Inc.",
        "tier": 1,
        "address": "Hamilton, ON, Canada",
        "city": "Hamilton",
        "country": "Canada",
        "latitude": 43.260,
        "longitude": -79.868,
        "primary_contact": "Diana Li",
        "email": "ops@northerncore.example",
        "phone": "+1 905 555 2121",
        "components": [
            {"name": "Electrical steel laminations", "capacity": 15000.0, "lead_time": 5.0, "moq": 4000.0},
        ],
    },
    {
        "name": "CopperCraft Windings Ltd.",
        "tier": 1,
        "address": "Detroit, MI, USA",
        "city": "Detroit",
        "country": "USA",
        "latitude": 42.331,
        "longitude": -83.046,
        "primary_contact": "Hector Morales",
        "email": "sales@coppercraft.example",
        "phone": "+1 313 555 9012",
        "components": [
            {"name": "Copper windings", "capacity": 9000.0, "lead_time": 6.0, "moq": 2000.0},
        ],
    },
    {
        "name": "Polar Oils & Fluids",
        "tier": 1,
        "address": "Sarnia, ON, Canada",
        "city": "Sarnia",
        "country": "Canada",
        "latitude": 42.974,
        "longitude": -82.403,
        "primary_contact": "Sameer Patel",
        "email": "logistics@polaroils.example",
        "phone": "+1 519 555 1289",
        "components": [
            {"name": "Insulating oil", "capacity": 25000.0, "lead_time": 4.0, "moq": 10000.0},
        ],
    },
    {
        "name": "AltaInsulate Components",
        "tier": 1,
        "address": "Malmö, Sweden",
        "city": "Malmö",
        "country": "Sweden",
        "latitude": 55.605,
        "longitude": 13.003,
        "primary_contact": "Eva Lindholm",
        "email": "support@altainsulate.example",
        "phone": "+46 40 555 6644",
        "components": [
            {"name": "Bushings HV", "capacity": 120.0, "lead_time": 14.0, "moq": 12.0},
        ],
    },
    {
        "name": "TapTech Drives GmbH",
        "tier": 1,
        "address": "Mannheim, Germany",
        "city": "Mannheim",
        "country": "Germany",
        "latitude": 49.487,
        "longitude": 8.466,
        "primary_contact": "Markus Hahn",
        "email": "orders@taptechdrives.example",
        "phone": "+49 621 555 7100",
        "components": [
            {"name": "On-load tap changer", "capacity": 60.0, "lead_time": 12.0, "moq": 1.0},
        ],
    },
    {
        "name": "GreatLakes Fabrication",
        "tier": 1,
        "address": "Toledo, OH, USA",
        "city": "Toledo",
        "country": "USA",
        "latitude": 41.663,
        "longitude": -83.555,
        "primary_contact": "Natalie Brooks",
        "email": "projects@greatlakesfab.example",
        "phone": "+1 419 555 2210",
        "components": [
            {"name": "Tank & Radiator assembly", "capacity": 90.0, "lead_time": 6.0, "moq": 1.0},
        ],
    },
    {
        "name": "Prairie Fasteners",
        "tier": 1,
        "address": "Regina, SK, Canada",
        "city": "Regina",
        "country": "Canada",
        "latitude": 50.445,
        "longitude": -104.618,
        "primary_contact": "Aaron Cooper",
        "email": "service@prairiefasteners.example",
        "phone": "+1 306 555 7711",
        "components": [
            {"name": "Core clamping hardware", "capacity": 300.0, "lead_time": 6.0, "moq": 10.0},
        ],
    },
    {
        "name": "NorthStar Controls",
        "tier": 1,
        "address": "Mississauga, ON, Canada",
        "city": "Mississauga",
        "country": "Canada",
        "latitude": 43.589,
        "longitude": -79.644,
        "primary_contact": "Priya Reddy",
        "email": "info@northstarcontrols.example",
        "phone": "+1 905 555 6640",
        "components": [
            {"name": "Control cabinet & protection relays", "capacity": 80.0, "lead_time": 5.0, "moq": 1.0},
        ],
    },
]

SAMPLE_SUPPLIERS_TIER2 = [
    {
        "name": "Baltic Steel Slitters",
        "tier": 2,
        "address": "Klaipėda, Lithuania",
        "city": "Klaipėda",
        "country": "Lithuania",
        "latitude": 55.703,
        "longitude": 21.144,
    },
    {
        "name": "Andes Copper Rod Co.",
        "tier": 2,
        "address": "Antofagasta, Chile",
        "city": "Antofagasta",
        "country": "Chile",
        "latitude": -23.650,
        "longitude": -70.400,
    },
    {
        "name": "NordChem Base Oils",
        "tier": 2,
        "address": "Gothenburg, Sweden",
        "city": "Gothenburg",
        "country": "Sweden",
        "latitude": 57.708,
        "longitude": 11.974,
    },
    {
        "name": "EuroCeramics HV",
        "tier": 2,
        "address": "Brno, Czechia",
        "city": "Brno",
        "country": "Czechia",
        "latitude": 49.195,
        "longitude": 16.607,
    },
    {
        "name": "Rhein Precision Gears",
        "tier": 2,
        "address": "Bonn, Germany",
        "city": "Bonn",
        "country": "Germany",
        "latitude": 50.737,
        "longitude": 7.098,
    },
    {
        "name": "Midwest Steel Plate",
        "tier": 2,
        "address": "Gary, IN, USA",
        "city": "Gary",
        "country": "USA",
        "latitude": 41.593,
        "longitude": -87.346,
    },
]

SAMPLE_FACILITY_COMPONENTS = [
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "Electrical steel laminations", "operation": "Stacking & assembly"},
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "Insulating oil", "operation": "Oil fill"},
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "Bushings HV", "operation": "Installation"},
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "On-load tap changer", "operation": "Integration"},
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "Tank & Radiator assembly", "operation": "Integration"},
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "Core clamping hardware", "operation": "Structural assembly"},
    {"facility": "Maple Grid Systems – Cambridge Final Assembly", "component": "Control cabinet & protection relays", "operation": "Controls integration"},
    {"facility": "Maple Grid Systems – Windsor Coil Shop", "component": "Electrical steel laminations", "operation": "Core prep"},
    {"facility": "Maple Grid Systems – Windsor Coil Shop", "component": "Copper windings", "operation": "Coil winding"},
]

SAMPLE_FLOWS = [
    {"from": "Baltic Steel Slitters", "to": "Northern Core Metals Inc.", "component": "Electrical steel laminations", "flow_type": "semi_finished", "lead_time": 16.0, "incoterms": "CFR"},
    {"from": "Northern Core Metals Inc.", "to": "Maple Grid Systems – Windsor Coil Shop", "component": "Electrical steel laminations", "flow_type": "component", "lead_time": 3.0, "incoterms": "DAP"},
    {"from": "Maple Grid Systems – Windsor Coil Shop", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Electrical steel laminations", "flow_type": "semi_finished", "lead_time": 2.0, "incoterms": "EXW"},
    {"from": "Andes Copper Rod Co.", "to": "CopperCraft Windings Ltd.", "component": "Copper windings", "flow_type": "semi_finished", "lead_time": 18.0, "incoterms": "CFR"},
    {"from": "CopperCraft Windings Ltd.", "to": "Maple Grid Systems – Windsor Coil Shop", "component": "Copper windings", "flow_type": "component", "lead_time": 5.0, "incoterms": "DAP"},
    {"from": "Maple Grid Systems – Windsor Coil Shop", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Copper windings", "flow_type": "semi_finished", "lead_time": 2.0, "incoterms": "EXW"},
    {"from": "NordChem Base Oils", "to": "Polar Oils & Fluids", "component": "Insulating oil", "flow_type": "semi_finished", "lead_time": 14.0, "incoterms": "FOB"},
    {"from": "Polar Oils & Fluids", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Insulating oil", "flow_type": "component", "lead_time": 3.0, "incoterms": "DAP"},
    {"from": "EuroCeramics HV", "to": "AltaInsulate Components", "component": "Bushings HV", "flow_type": "semi_finished", "lead_time": 17.0, "incoterms": "FCA"},
    {"from": "AltaInsulate Components", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Bushings HV", "flow_type": "component", "lead_time": 9.0, "incoterms": "DAP"},
    {"from": "Rhein Precision Gears", "to": "TapTech Drives GmbH", "component": "On-load tap changer", "flow_type": "semi_finished", "lead_time": 12.0, "incoterms": "FCA"},
    {"from": "TapTech Drives GmbH", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "On-load tap changer", "flow_type": "component", "lead_time": 10.0, "incoterms": "DAP"},
    {"from": "Midwest Steel Plate", "to": "GreatLakes Fabrication", "component": "Tank & Radiator assembly", "flow_type": "semi_finished", "lead_time": 8.0, "incoterms": "FOB"},
    {"from": "GreatLakes Fabrication", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Tank & Radiator assembly", "flow_type": "component", "lead_time": 4.0, "incoterms": "DAP"},
    {"from": "Prairie Fasteners", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Core clamping hardware", "flow_type": "component", "lead_time": 6.0, "incoterms": "CIP"},
    {"from": "NorthStar Controls", "to": "Maple Grid Systems – Cambridge Final Assembly", "component": "Control cabinet & protection relays", "flow_type": "finished", "lead_time": 5.0, "incoterms": "DAP"},
]


# ------------------------------------------------------------------------------
# Sample data loader
# ------------------------------------------------------------------------------

def ensure_sample_data(session: Session) -> None:
    existing = session.execute(select(Product).where(Product.name == SAMPLE_PRODUCT["name"])).scalar_one_or_none()
    if existing:
        return

    product = Product(
        name=SAMPLE_PRODUCT["name"],
        client_code=SAMPLE_PRODUCT["client_code"],
        description=SAMPLE_PRODUCT["description"],
        lifecycle_status=SAMPLE_PRODUCT["lifecycle_status"],
    )
    session.add(product)
    session.flush()

    components: Dict[str, Component] = {}
    for entry in SAMPLE_COMPONENTS:
        component = Component(
            product_id=product.id,
            name=entry["name"],
            qty_per_product=entry["qty"],
            uom=entry["uom"],
        )
        session.add(component)
        components[component.name] = component
    session.flush()

    facilities: Dict[str, Facility] = {}
    for entry in SAMPLE_FACILITIES:
        facility = Facility(
            name=entry["name"],
            facility_type=entry["type"],
            address=entry["address"],
            city=entry["city"],
            country=entry["country"],
            latitude=entry["latitude"],
            longitude=entry["longitude"],
        )
        facility.set_operations(entry.get("operations", []))
        session.add(facility)
        session.flush()
        ensure_supply_node_for_facility(session, facility)
        facilities[facility.name] = facility
    session.flush()

    suppliers: Dict[str, Supplier] = {}
    for entry in [*SAMPLE_SUPPLIERS_TIER1, *SAMPLE_SUPPLIERS_TIER2]:
        supplier = Supplier(
            name=entry["name"],
            tier=entry["tier"],
            address=entry.get("address", ""),
            city=entry["city"],
            country=entry["country"],
            latitude=entry["latitude"],
            longitude=entry["longitude"],
            primary_contact=entry.get("primary_contact", ""),
            email=entry.get("email", ""),
            phone=entry.get("phone", ""),
        )
        session.add(supplier)
        session.flush()
        ensure_supply_node_for_supplier(session, supplier)
        suppliers[supplier.name] = supplier
    session.flush()

    for entry in SAMPLE_SUPPLIERS_TIER1:
        supplier = suppliers[entry["name"]]
        for component_info in entry.get("components", []):
            component = components.get(component_info["name"])
            if component is None:
                continue
            link = SupplierComponent(
                supplier_id=supplier.id,
                component_id=component.id,
                capacity_units_per_month=component_info.get("capacity"),
                moq=component_info.get("moq"),
                lead_time_days=component_info.get("lead_time"),
            )
            session.add(link)
    session.flush()

    for entry in SAMPLE_FACILITY_COMPONENTS:
        facility = facilities.get(entry["facility"])
        component = components.get(entry["component"])
        if facility is None or component is None:
            continue
        link = FacilityComponent(
            facility_id=facility.id,
            component_id=component.id,
            operation=entry.get("operation", ""),
        )
        session.add(link)
    session.flush()

    nodes_by_name: Dict[str, SupplyNode] = {}
    nodes_by_name.update({name: suppliers[name].supply_node for name in suppliers})
    nodes_by_name.update({name: facilities[name].supply_node for name in facilities})

    for entry in SAMPLE_FLOWS:
        source = nodes_by_name.get(entry["from"])
        target = nodes_by_name.get(entry["to"])
        if source is None or target is None:
            continue
        component = components.get(entry.get("component", ""))
        flow = MaterialFlow(
            from_node_id=source.id,
            to_node_id=target.id,
            component_id=component.id if component else None,
            flow_type=entry.get("flow_type", "component"),
            lead_time_days=entry.get("lead_time"),
            incoterms=entry.get("incoterms", ""),
            notes=entry.get("notes", ""),
        )
        session.add(flow)


# ------------------------------------------------------------------------------
# Database initialization helpers
# ------------------------------------------------------------------------------

DB_PATH = Path(__file__).with_name("supply_chain.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"


@st.cache_resource
def get_session_factory() -> sessionmaker:
    engine = create_engine(DATABASE_URL, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


SessionFactory = get_session_factory()


@st.cache_resource
def seed_database() -> bool:
    with SessionFactory() as session:
        ensure_sample_data(session)
        session.commit()
    return True


seed_database()


# ------------------------------------------------------------------------------
# Filtering and map data structures
# ------------------------------------------------------------------------------

NODE_COLOR_BY_TIER: Dict[int, List[int]] = {
    1: [31, 119, 180, 220],
    2: [102, 194, 255, 220],
    3: [198, 219, 239, 220],
}

FACILITY_COLOR_BY_TYPE: Dict[str, List[int]] = {
    "assembly": [255, 127, 14, 230],
    "sub_assembly": [253, 216, 53, 230],
}

FLOW_COLOR_BY_TYPE: Dict[str, List[int]] = {
    "component": [0, 150, 136, 200],
    "semi_finished": [142, 45, 197, 200],
    "finished": [56, 142, 60, 200],
}

NODE_RADIUS_BY_TIER = {1: 65000, 2: 55000, 3: 45000}
FACILITY_RADIUS = 70000
MAP_STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
ARROW_ICON_URL = "https://raw.githubusercontent.com/visgl/deck.gl-data/master/icon/arrow.png"


@dataclass
class FilterCriteria:
    product_ids: Set[str]
    component_ids: Set[str]
    tier_levels: Set[int]
    countries: Set[str]
    include_subtiers: bool
    selected_product_names: List[str]
    selected_component_names: List[str]


@dataclass
class MapData:
    nodes_df: pd.DataFrame
    flows_df: pd.DataFrame
    arrow_df: pd.DataFrame
    nodes_by_id: Dict[str, SupplyNode]
    flows: List[MaterialFlow]
    missing_coordinates: List[str]


# ------------------------------------------------------------------------------
# Filter controls
# ------------------------------------------------------------------------------

def render_filters(session: Session) -> FilterCriteria:
    st.sidebar.header("Filters")

    products = session.execute(select(Product).order_by(Product.name)).scalars().all()
    product_options = {product.name: product.id for product in products}
    selected_product_names = st.sidebar.multiselect("Product", list(product_options.keys()))
    selected_product_ids = {product_options[name] for name in selected_product_names}

    components_stmt = select(Component).order_by(Component.name)
    if selected_product_ids:
        components_stmt = components_stmt.where(Component.product_id.in_(selected_product_ids))
    components = session.execute(components_stmt).scalars().all()
    component_options = {component.name: component.id for component in components}
    selected_component_names = st.sidebar.multiselect("Component", list(component_options.keys()))

    if selected_component_names:
        selected_component_ids = {component_options[name] for name in selected_component_names}
        component_names_display = list(selected_component_names)
    elif selected_product_ids:
        selected_component_ids = set(component_options.values())
        component_names_display = list(component_options.keys())
    else:
        selected_component_ids = set()
        component_names_display = []

    tier_values = session.execute(select(Supplier.tier).distinct()).scalars().all()
    available_tiers = sorted({tier for tier in tier_values if tier is not None})
    tier_labels = [f"Tier {tier}" for tier in available_tiers]
    selected_tier_labels = st.sidebar.multiselect("Supplier Tier", tier_labels, default=tier_labels)
    tier_levels = {available_tiers[tier_labels.index(label)] for label in selected_tier_labels} if selected_tier_labels else set(available_tiers)

    include_subtiers = st.sidebar.checkbox("Show sub-tiers", value=True)
    if not include_subtiers:
        tier_levels = {tier for tier in tier_levels if tier == 1} or {1}

    country_values = session.execute(select(SupplyNode.country).distinct()).scalars().all()
    countries = sorted({country for country in country_values if country})
    selected_countries = set(st.sidebar.multiselect("Country", countries))

    if st.sidebar.button("Reset Filters"):
        st.experimental_rerun()

    return FilterCriteria(
        product_ids=selected_product_ids,
        component_ids=selected_component_ids,
        tier_levels=tier_levels,
        countries=selected_countries,
        include_subtiers=include_subtiers,
        selected_product_names=list(selected_product_names),
        selected_component_names=component_names_display,
    )


# ------------------------------------------------------------------------------
# Map data preparation
# ------------------------------------------------------------------------------

def summarize_lead_times(node: SupplyNode, flows: Sequence[MaterialFlow]) -> str:
    durations = []
    for flow in flows:
        if flow.from_node_id == node.id and flow.lead_time_days:
            target = flow.to_node
            if target and target.node_type == "facility":
                durations.append(flow.lead_time_days)
    if not durations:
        return ""
    minimum = min(durations)
    maximum = max(durations)
    if math.isclose(minimum, maximum):
        return f"{minimum:.0f} d"
    return f"{minimum:.0f}–{maximum:.0f} d"


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360.0) % 360.0


def intermediate_point(lat1: float, lon1: float, lat2: float, lon2: float, fraction: float = 0.5) -> Tuple[float, float]:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    lambda1 = math.radians(lon1)
    lambda2 = math.radians(lon2)

    delta = 2 * math.asin(
        math.sqrt(
            math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin((lambda2 - lambda1) / 2) ** 2
        )
    )
    if math.isclose(delta, 0.0):
        return lat1, lon1

    a = math.sin((1 - fraction) * delta) / math.sin(delta)
    b = math.sin(fraction * delta) / math.sin(delta)

    x = a * math.cos(phi1) * math.cos(lambda1) + b * math.cos(phi2) * math.cos(lambda2)
    y = a * math.cos(phi1) * math.sin(lambda1) + b * math.cos(phi2) * math.sin(lambda2)
    z = a * math.sin(phi1) + b * math.sin(phi2)

    phi_mid = math.atan2(z, math.sqrt(x * x + y * y))
    lambda_mid = math.atan2(y, x)
    return math.degrees(phi_mid), math.degrees(lambda_mid)


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def destination_point(lat: float, lon: float, bearing: float, distance_km: float) -> Tuple[float, float]:
    radius = 6371.0
    delta = distance_km / radius
    theta = math.radians(bearing)

    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)

    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(sin_phi2)

    y = math.sin(theta) * math.sin(delta) * math.cos(phi1)
    x = math.cos(delta) - math.sin(phi1) * sin_phi2
    lambda2 = lambda1 + math.atan2(y, x)

    return math.degrees(phi2), math.degrees(lambda2)


def collect_visual_data(session: Session, filters: FilterCriteria) -> MapData:
    all_nodes = session.execute(select(SupplyNode).order_by(SupplyNode.name)).scalars().all()
    nodes_by_id = {node.id: node for node in all_nodes}

    relevant_node_ids: Set[str]
    if filters.component_ids:
        relevant_node_ids = set()
        supplier_links = session.execute(
            select(SupplierComponent).where(SupplierComponent.component_id.in_(filters.component_ids))
        ).scalars().all()
        for link in supplier_links:
            supplier = link.supplier
            if supplier and supplier.supply_node_id:
                relevant_node_ids.add(supplier.supply_node_id)
        facility_links = session.execute(
            select(FacilityComponent).where(FacilityComponent.component_id.in_(filters.component_ids))
        ).scalars().all()
        for link in facility_links:
            facility = link.facility
            if facility and facility.supply_node_id:
                relevant_node_ids.add(facility.supply_node_id)
    else:
        relevant_node_ids = {node.id for node in all_nodes}

    flows_stmt = select(MaterialFlow)
    if filters.component_ids:
        flows_stmt = flows_stmt.where(MaterialFlow.component_id.in_(filters.component_ids))
    flows = session.execute(flows_stmt).scalars().all()

    filtered_flows: List[MaterialFlow] = []
    arrow_records: List[Dict[str, object]] = []
    for flow in flows:
        source = nodes_by_id.get(flow.from_node_id)
        target = nodes_by_id.get(flow.to_node_id)
        if source is None or target is None:
            continue

        if source.node_type == "supplier":
            tier = source.tier or 0
            if filters.tier_levels and tier not in filters.tier_levels:
                continue
            if not filters.include_subtiers and tier > 1:
                continue
        if target.node_type == "supplier":
            tier = target.tier or 0
            if filters.tier_levels and tier not in filters.tier_levels:
                continue
            if not filters.include_subtiers and tier > 1:
                continue

        if filters.countries:
            if source.country not in filters.countries and target.country not in filters.countries:
                continue

        filtered_flows.append(flow)
        relevant_node_ids.add(source.id)
        relevant_node_ids.add(target.id)

    node_records: List[Dict[str, object]] = []
    missing_coordinates: List[str] = []
    for node in all_nodes:
        if node.id not in relevant_node_ids:
            continue
        if filters.countries and node.country not in filters.countries:
            continue
        if node.node_type == "supplier":
            tier = node.tier or 0
            if filters.tier_levels and tier not in filters.tier_levels:
                continue
            if not filters.include_subtiers and tier > 1:
                continue
        if node.latitude is None or node.longitude is None:
            missing_coordinates.append(node.name)
            continue

        if node.node_type == "supplier":
            tier = node.tier or 0
            color = NODE_COLOR_BY_TIER.get(tier, [180, 180, 180, 220])
            radius = NODE_RADIUS_BY_TIER.get(tier, 50000)
            role = f"Supplier Tier-{tier}" if tier else "Supplier"
            supplier = node.supplier
            components_list = sorted(
                {link.component.name for link in (supplier.components if supplier else []) if link.component}
            )
        else:
            facility = node.facility
            f_type = (facility.facility_type if facility else "assembly").lower()
            color = FACILITY_COLOR_BY_TYPE.get(f_type, [255, 193, 7, 220])
            radius = FACILITY_RADIUS
            role = f"{f_type.replace('_', ' ').title()} Facility"
            components_list = sorted(
                {link.component.name for link in (facility.components if facility else []) if link.component}
            )

        node_records.append(
            {
                "id": node.id,
                "name": node.name,
                "role": role,
                "city": f"{node.city}, {node.country}" if node.city else node.country,
                "country": node.country,
                "latitude": node.latitude,
                "longitude": node.longitude,
                "tier": node.tier,
                "components": ", ".join(components_list) if components_list else "—",
                "lead_time_summary": summarize_lead_times(node, filtered_flows) or "—",
                "color": color,
                "radius": radius,
            }
        )

    flow_records: List[Dict[str, object]] = []
    for flow in filtered_flows:
        source = nodes_by_id.get(flow.from_node_id)
        target = nodes_by_id.get(flow.to_node_id)
        if source is None or target is None:
            continue
        if source.latitude is None or source.longitude is None:
            continue
        if target.latitude is None or target.longitude is None:
            continue
        color = FLOW_COLOR_BY_TYPE.get(flow.flow_type, FLOW_COLOR_BY_TYPE["component"])
        bearing = calculate_bearing(source.latitude, source.longitude, target.latitude, target.longitude)
        mid_lat, mid_lon = intermediate_point(source.latitude, source.longitude, target.latitude, target.longitude, fraction=0.5)
        distance_km = haversine_distance_km(source.latitude, source.longitude, target.latitude, target.longitude)
        if distance_km > 0.1:
            base_fraction = max(0.55, 1.0 - (120.0 / max(distance_km, 1.0)))
            base_lat, base_lon = intermediate_point(source.latitude, source.longitude, target.latitude, target.longitude, fraction=base_fraction)
            arrow_length = max(min(distance_km * 0.2, 600.0), 40.0)
            wing_length = arrow_length * 0.35
            left_lat, left_lon = destination_point(base_lat, base_lon, bearing + 140.0, wing_length)
            right_lat, right_lon = destination_point(base_lat, base_lon, bearing - 140.0, wing_length)
            arrow_records.append(
                {
                    "id": flow.id,
                    "polygon": [
                        [target.longitude, target.latitude],
                        [left_lon, left_lat],
                        [right_lon, right_lat],
                    ],
                    "color": color,
                }
            )

        flow_records.append(
            {
                "id": flow.id,
                "from_name": source.name,
                "to_name": target.name,
                "component": flow.component.name if flow.component else "—",
                "flow_type": flow.flow_type,
                "lead_time": float(flow.lead_time_days or 0.0),
                "incoterms": flow.incoterms or "—",
                "notes": flow.notes or "",
                "source_lon": source.longitude,
                "source_lat": source.latitude,
                "target_lon": target.longitude,
                "target_lat": target.latitude,
                "color": color,
                "mid_lon": mid_lon,
                "mid_lat": mid_lat,
                "icon_name": "arrow",
                "bearing": bearing,
                "angle": (bearing - 90.0) % 360.0,
            }
        )

    nodes_df = pd.DataFrame(node_records)
    flows_df = pd.DataFrame(flow_records)
    arrow_df = pd.DataFrame(arrow_records)
    return MapData(
        nodes_df=nodes_df,
        flows_df=flows_df,
        arrow_df=arrow_df,
        nodes_by_id=nodes_by_id,
        flows=filtered_flows,
        missing_coordinates=missing_coordinates,
    )


def compute_view_state(nodes_df: pd.DataFrame) -> pdk.ViewState:
    if nodes_df.empty:
        return pdk.ViewState(latitude=20.0, longitude=0.0, zoom=1.5, pitch=35)
    mean_lat = float(nodes_df["latitude"].mean())
    mean_lon = float(nodes_df["longitude"].mean())
    lat_span = float(nodes_df["latitude"].max() - nodes_df["latitude"].min())
    lon_span = float(nodes_df["longitude"].max() - nodes_df["longitude"].min())
    span = max(lat_span, lon_span, 1.0)
    if span > 120:
        zoom = 2.0
    elif span > 80:
        zoom = 2.5
    elif span > 40:
        zoom = 3.5
    elif span > 20:
        zoom = 4.2
    else:
        zoom = 5.0
    return pdk.ViewState(latitude=mean_lat, longitude=mean_lon, zoom=zoom, pitch=35)


def render_map(map_data: MapData) -> None:
    if map_data.nodes_df.empty:
        st.info("No network nodes match the current filters.")
        return

    node_layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_data.nodes_df,
        get_position=["longitude", "latitude"],
        get_fill_color="color",
        get_radius="radius",
        pickable=True,
        radius_min_pixels=6,
        radius_max_pixels=60,
        stroked=True,
        get_line_color=[0, 0, 0, 80],
        tooltip={
            "html": "<b>{name}</b><br/>{role}<br/>{city}<br/><b>Components:</b> {components}<br/><b>Lead time:</b> {lead_time_summary}",
            "style": {"backgroundColor": "#1f2630", "color": "white"},
        },
    )

    flow_layer = pdk.Layer(
        "ArcLayer",
        data=map_data.flows_df,
        get_source_position=["source_lon", "source_lat"],
        get_target_position=["target_lon", "target_lat"],
        get_source_color="color",
        get_target_color="color",
        get_width=4,
        pickable=True,
        tooltip={
            "html": "<b>{from_name} → {to_name}</b><br/><b>Component:</b> {component}<br/><b>Flow:</b> {flow_type}<br/><b>Lead time:</b> {lead_time} d<br/><b>Incoterms:</b> {incoterms}",
            "style": {"backgroundColor": "#1f2630", "color": "white"},
        },
    )

    layers = [flow_layer]
    if not map_data.arrow_df.empty:
        arrow_layer = pdk.Layer(
            "PolygonLayer",
            data=map_data.arrow_df,
            get_polygon="polygon",
            get_fill_color="color",
            get_line_color="color",
            line_width_min_pixels=0,
            opacity=0.85,
            pickable=False,
        )
        layers.append(arrow_layer)
    layers.append(node_layer)

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=compute_view_state(map_data.nodes_df),
        map_style=MAP_STYLE_URL,
    )
    st.pydeck_chart(deck)


# ------------------------------------------------------------------------------
# Summary and analytics
# ------------------------------------------------------------------------------

def compute_longest_lead_path(map_data: MapData) -> Tuple[float, List[MaterialFlow]]:
    outgoing: Dict[str, List[MaterialFlow]] = defaultdict(list)
    for flow in map_data.flows:
        outgoing[flow.from_node_id].append(flow)

    memo: Dict[str, Tuple[float, Optional[MaterialFlow]]] = {}

    def dfs(node_id: str, visiting: Set[str]) -> Tuple[float, Optional[MaterialFlow]]:
        if node_id in memo:
            return memo[node_id]
        visiting.add(node_id)
        best: Tuple[float, Optional[MaterialFlow]] = (0.0, None)
        for flow in outgoing.get(node_id, []):
            if flow.to_node_id in visiting:
                continue
            lead = float(flow.lead_time_days or 0.0)
            tail_length, _ = dfs(flow.to_node_id, visiting)
            total = lead + tail_length
            if total > best[0]:
                best = (total, flow)
        visiting.remove(node_id)
        memo[node_id] = best
        return best

    best_total = 0.0
    best_start: Optional[str] = None
    for node_id in outgoing.keys():
        total, _ = dfs(node_id, set())
        if total > best_total:
            best_total = total
            best_start = node_id

    path: List[MaterialFlow] = []
    if best_start:
        current = best_start
        while True:
            info = memo.get(current)
            if info is None or info[1] is None:
                break
            flow = info[1]
            path.append(flow)
            current = flow.to_node_id
    return best_total, path


def build_bom_coverage_table(session: Session, filters: FilterCriteria) -> pd.DataFrame:
    components_stmt = select(Component).order_by(Component.name)
    if filters.component_ids:
        components_stmt = components_stmt.where(Component.id.in_(filters.component_ids))
    elif filters.product_ids:
        components_stmt = components_stmt.where(Component.product_id.in_(filters.product_ids))
    components = session.execute(components_stmt).scalars().all()

    rows: List[Dict[str, object]] = []
    for component in components:
        suppliers = [link.supplier for link in component.supplier_links if link.supplier]
        tier1_suppliers = [supplier for supplier in suppliers if (supplier.tier or 0) == 1]
        supplier_names = ", ".join(sorted({supplier.name for supplier in suppliers})) or "—"
        tier1_names = ", ".join(sorted({supplier.name for supplier in tier1_suppliers})) or "—"
        supplier_count = len({supplier.id for supplier in suppliers})
        if supplier_count == 0:
            status = "Missing"
        elif supplier_count == 1:
            status = "Single-source"
        elif supplier_count == 2:
            status = "Dual-source"
        else:
            status = "Multi-source"
        rows.append(
            {
                "Component": component.name,
                "UoM": component.uom,
                "Qty per product": component.qty_per_product,
                "Sourcing status": status,
                "Tier-1 suppliers": tier1_names,
                "All suppliers": supplier_names,
            }
        )
    return pd.DataFrame(rows)


def render_summary(session: Session, filters: FilterCriteria, map_data: MapData) -> None:
    st.subheader("Summary")
    product_line = ", ".join(filters.selected_product_names) if filters.selected_product_names else "All products"
    component_line = ", ".join(filters.selected_component_names) if filters.selected_component_names else "All components"
    st.markdown(f"**Products:** {product_line}")
    st.markdown(f"**Components:** {component_line}")

    component_stmt = select(Component)
    if filters.component_ids:
        component_stmt = component_stmt.where(Component.id.in_(filters.component_ids))
    elif filters.product_ids:
        component_stmt = component_stmt.where(Component.product_id.in_(filters.product_ids))
    component_results = session.execute(component_stmt).scalars().unique().all()
    component_count = len(component_results)

    supplier_rows = map_data.nodes_df[map_data.nodes_df["role"].str.startswith("Supplier")]
    tier1_count = int((supplier_rows["tier"] == 1).sum()) if not supplier_rows.empty else 0
    total_supplier_count = int(len(supplier_rows))
    country_count = int(map_data.nodes_df["country"].nunique()) if not map_data.nodes_df.empty else 0

    col1, col2 = st.columns(2)
    col1.metric("Components", component_count)
    col1.metric("Tier-1 Suppliers", tier1_count)
    col2.metric("Suppliers (All Tiers)", total_supplier_count)
    col2.metric("Countries", country_count)

    longest_days, path_flows = compute_longest_lead_path(map_data)
    if path_flows:
        nodes_sequence = [map_data.nodes_by_id[path_flows[0].from_node_id].name]
        nodes_sequence.extend(map_data.nodes_by_id[flow.to_node_id].name for flow in path_flows)
        st.caption(f"Longest lead-time path ≈ {longest_days:.0f} days: {' → '.join(nodes_sequence)}")
    else:
        st.caption("No lead-time path available for the current selection.")

    st.markdown("**BOM Coverage**")
    coverage_df = build_bom_coverage_table(session, filters)
    if coverage_df.empty:
        st.write("No components available for the current selection.")
    else:
        st.dataframe(coverage_df, use_container_width=True, hide_index=True)

    st.markdown("**Node Detail**")
    node_options = ["—"] + map_data.nodes_df["name"].tolist()
    selected_node = st.selectbox("Inspect node", node_options, key="node_detail_select")
    if selected_node and selected_node != "—":
        render_node_detail(session, selected_node, map_data)

    st.markdown("**Flow Detail**")
    flow_options = ["—"] + [f"{row['from_name']} → {row['to_name']} ({row['component']})" for _, row in map_data.flows_df.iterrows()]
    selected_flow = st.selectbox("Inspect flow", flow_options, key="flow_detail_select")
    if selected_flow and selected_flow != "—":
        render_flow_detail(session, selected_flow, map_data)


def render_node_detail(session: Session, node_name: str, map_data: MapData) -> None:
    node = session.execute(select(SupplyNode).where(SupplyNode.name == node_name)).scalar_one_or_none()
    if node is None:
        st.write("Node not found.")
        return
    st.write(f"**{node.name}**")
    st.write(f"Type: {node.node_type.title()}" + (f" | Tier-{node.tier}" if node.node_type == "supplier" and node.tier else ""))
    st.write(f"Location: {node.city}, {node.country}")
    if node.node_type == "supplier" and node.supplier:
        supplier = node.supplier
        st.write(f"Contact: {supplier.primary_contact or '—'}")
        st.write(f"Email: {supplier.email or '—'}")
        st.write(f"Phone: {supplier.phone or '—'}")
    if node.node_type == "facility" and node.facility:
        operations = node.facility.operations_list()
        st.write(f"Operations: {', '.join(operations) if operations else '—'}")

    outgoing = [flow for flow in map_data.flows if flow.from_node_id == node.id]
    incoming = [flow for flow in map_data.flows if flow.to_node_id == node.id]
    if outgoing:
        st.write("Outbound flows:")
        for flow in outgoing:
            dest = map_data.nodes_by_id.get(flow.to_node_id)
            st.write(
                f"- To {dest.name if dest else 'Unknown'} ({flow.flow_type}) "
                f"component: {flow.component.name if flow.component else '—'} "
                f"lead: {flow.lead_time_days or 0:.0f} d"
            )
    if incoming:
        st.write("Inbound flows:")
        for flow in incoming:
            src = map_data.nodes_by_id.get(flow.from_node_id)
            st.write(
                f"- From {src.name if src else 'Unknown'} ({flow.flow_type}) "
                f"component: {flow.component.name if flow.component else '—'} "
                f"lead: {flow.lead_time_days or 0:.0f} d"
            )


def render_flow_detail(session: Session, label: str, map_data: MapData) -> None:
    for flow in map_data.flows:
        source = map_data.nodes_by_id.get(flow.from_node_id)
        target = map_data.nodes_by_id.get(flow.to_node_id)
        descriptor = f"{source.name if source else '?'} → {target.name if target else '?'} ({flow.component.name if flow.component else '—'})"
        if descriptor == label:
            st.write(f"**{descriptor}**")
            st.write(f"Flow type: {flow.flow_type}")
            st.write(f"Lead time: {flow.lead_time_days or 0:.0f} days")
            st.write(f"Incoterms: {flow.incoterms or '—'}")
            st.write(f"Notes: {flow.notes or '—'}")
            return
    st.write("Flow not found.")


# ------------------------------------------------------------------------------
# CRUD forms
# ------------------------------------------------------------------------------

def render_crud_forms(session: Session) -> None:
    st.sidebar.subheader("Manage Network Data")
    render_product_form(session)
    render_component_form(session)
    render_supplier_form(session)
    render_facility_form(session)
    render_flow_form(session)


def render_product_form(session: Session) -> None:
    products = session.execute(select(Product).order_by(Product.name)).scalars().all()
    product_map = {product.name: product for product in products}
    options = ["New product"] + list(product_map.keys())
    with st.sidebar.expander("Add / Edit Product"):
        selected_label = st.selectbox("Select product", options, key="product_select")
        editing = product_map.get(selected_label)
        lifecycle_options = ["active", "prototype", "obsolete"]
        lifecycle_default = lifecycle_options.index(editing.lifecycle_status) if editing and editing.lifecycle_status in lifecycle_options else 0

        with st.form("product_form"):
            name = st.text_input("Name", value=editing.name if editing else "")
            client_code = st.text_input("Client code", value=editing.client_code or "" if editing else "")
            lifecycle = st.selectbox("Lifecycle status", lifecycle_options, index=lifecycle_default)
            description = st.text_area("Description", value=editing.description if editing else "", height=80)
            submitted = st.form_submit_button("Save product")
            if submitted:
                if not name.strip():
                    st.warning("Product name is required.")
                    return
                if editing:
                    editing.name = name.strip()
                    editing.client_code = client_code.strip() or None
                    editing.lifecycle_status = lifecycle
                    editing.description = description.strip()
                else:
                    product = Product(
                        name=name.strip(),
                        client_code=client_code.strip() or None,
                        lifecycle_status=lifecycle,
                        description=description.strip(),
                    )
                    session.add(product)
                session.commit()
                st.success("Product saved.")
                st.experimental_rerun()


def render_component_form(session: Session) -> None:
    products = session.execute(select(Product).order_by(Product.name)).scalars().all()
    product_map = {product.name: product for product in products}
    product_options = list(product_map.keys())

    components = session.execute(select(Component).order_by(Component.name)).scalars().all()
    component_map = {component.name: component for component in components}
    options = ["New component"] + list(component_map.keys())

    with st.sidebar.expander("Add / Edit Component"):
        selected_label = st.selectbox("Select component", options, key="component_select")
        editing = component_map.get(selected_label)
        default_product = editing.product.name if editing else (product_options[0] if product_options else "")
        with st.form("component_form"):
            if not product_options:
                st.info("Please create a product first.")
                st.form_submit_button("Save component", disabled=True)
                return
            product_name = st.selectbox("Product", product_options, index=product_options.index(default_product))
            name = st.text_input("Component name", value=editing.name if editing else "")
            uom = st.text_input("Unit of measure", value=editing.uom if editing else "")
            qty = st.number_input("Quantity per product", min_value=0.0, value=float(editing.qty_per_product if editing else 1.0), step=1.0)
            spec_ref = st.text_input("Specification reference", value=editing.spec_ref if editing else "")
            notes = st.text_area("Notes", value=editing.notes if editing else "", height=80)
            submitted = st.form_submit_button("Save component")
            if submitted:
                if not name.strip():
                    st.warning("Component name is required.")
                    return
                product = product_map[product_name]
                if editing:
                    editing.name = name.strip()
                    editing.product_id = product.id
                    editing.uom = uom.strip()
                    editing.qty_per_product = qty
                    editing.spec_ref = spec_ref.strip()
                    editing.notes = notes.strip()
                else:
                    component = Component(
                        product_id=product.id,
                        name=name.strip(),
                        uom=uom.strip(),
                        qty_per_product=qty,
                        spec_ref=spec_ref.strip(),
                        notes=notes.strip(),
                    )
                    session.add(component)
                session.commit()
                st.success("Component saved.")
                st.experimental_rerun()


def render_supplier_form(session: Session) -> None:
    suppliers = session.execute(select(Supplier).order_by(Supplier.name)).scalars().all()
    supplier_map = {supplier.name: supplier for supplier in suppliers}
    options = ["New supplier"] + list(supplier_map.keys())

    components = session.execute(select(Component).order_by(Component.name)).scalars().all()
    component_options = {
        f"{component.name} ({component.product.client_code or component.product.name})": component.id for component in components
    }

    with st.sidebar.expander("Add / Edit Supplier"):
        selected_label = st.selectbox("Select supplier", options, key="supplier_select")
        editing = supplier_map.get(selected_label)
        with st.form("supplier_form"):
            name = st.text_input("Name", value=editing.name if editing else "")
            tier = st.number_input("Tier", min_value=1, max_value=5, step=1, value=int(editing.tier if editing else 1))
            city = st.text_input("City", value=editing.city if editing else "")
            country = st.text_input("Country", value=editing.country if editing else "")
            address = st.text_area("Address", value=editing.address if editing else "", height=60)
            latitude = st.number_input("Latitude", min_value=-90.0, max_value=90.0, value=float(editing.latitude if editing else 0.0), format="%.6f")
            longitude = st.number_input("Longitude", min_value=-180.0, max_value=180.0, value=float(editing.longitude if editing else 0.0), format="%.6f")
            contact = st.text_input("Primary contact", value=editing.primary_contact if editing else "")
            email = st.text_input("Email", value=editing.email if editing else "")
            phone = st.text_input("Phone", value=editing.phone if editing else "")
            default_selected = []
            if editing:
                default_selected = [
                    label for label, component_id in component_options.items() if any(link.component_id == component_id for link in editing.components)
                ]
            selected_components = st.multiselect("Components supplied", list(component_options.keys()), default=default_selected)
            capacity_default = 0.0
            lead_time_default = 0.0
            moq_default = 0.0
            if editing and editing.components:
                lead_time_default = float(editing.components[0].lead_time_days or 0.0)
                capacity_default = float(editing.components[0].capacity_units_per_month or 0.0)
                moq_default = float(editing.components[0].moq or 0.0)
            capacity = st.number_input("Default capacity / month (units)", min_value=0.0, value=capacity_default, step=100.0)
            lead_time = st.number_input("Default lead time (days)", min_value=0.0, value=lead_time_default, step=1.0)
            moq = st.number_input("Default MOQ", min_value=0.0, value=moq_default, step=10.0)
            submitted = st.form_submit_button("Save supplier")
            if submitted:
                if not name.strip():
                    st.warning("Supplier name is required.")
                    return
                if editing:
                    supplier = editing
                    supplier.name = name.strip()
                    supplier.tier = int(tier)
                    supplier.city = city.strip()
                    supplier.country = country.strip()
                    supplier.address = address.strip()
                    supplier.latitude = latitude
                    supplier.longitude = longitude
                    supplier.primary_contact = contact.strip()
                    supplier.email = email.strip()
                    supplier.phone = phone.strip()
                else:
                    supplier = Supplier(
                        name=name.strip(),
                        tier=int(tier),
                        city=city.strip(),
                        country=country.strip(),
                        address=address.strip(),
                        latitude=latitude,
                        longitude=longitude,
                        primary_contact=contact.strip(),
                        email=email.strip(),
                        phone=phone.strip(),
                    )
                    session.add(supplier)
                    session.flush()
                ensure_supply_node_for_supplier(session, supplier)

                selected_component_ids = {component_options[label] for label in selected_components}
                existing_links = {link.component_id: link for link in supplier.components}
                for component_id in selected_component_ids:
                    link = existing_links.get(component_id)
                    cap_val = capacity if capacity > 0 else None
                    lt_val = lead_time if lead_time > 0 else None
                    moq_val = moq if moq > 0 else None
                    if link:
                        link.capacity_units_per_month = cap_val
                        link.lead_time_days = lt_val
                        link.moq = moq_val
                    else:
                        session.add(
                            SupplierComponent(
                                supplier_id=supplier.id,
                                component_id=component_id,
                                capacity_units_per_month=cap_val,
                                lead_time_days=lt_val,
                                moq=moq_val,
                            )
                        )
                for component_id, link in existing_links.items():
                    if component_id not in selected_component_ids:
                        session.delete(link)

                session.commit()
                st.success("Supplier saved.")
                st.experimental_rerun()


def render_facility_form(session: Session) -> None:
    facilities = session.execute(select(Facility).order_by(Facility.name)).scalars().all()
    facility_map = {facility.name: facility for facility in facilities}
    options = ["New facility"] + list(facility_map.keys())

    components = session.execute(select(Component).order_by(Component.name)).scalars().all()
    component_options = {component.name: component.id for component in components}

    with st.sidebar.expander("Add / Edit Facility"):
        selected_label = st.selectbox("Select facility", options, key="facility_select")
        editing = facility_map.get(selected_label)
        default_type = editing.facility_type if editing else "assembly"

        with st.form("facility_form"):
            name = st.text_input("Name", value=editing.name if editing else "")
            facility_type = st.selectbox("Type", ["assembly", "sub_assembly"], index=0 if default_type == "assembly" else 1)
            city = st.text_input("City", value=editing.city if editing else "")
            country = st.text_input("Country", value=editing.country if editing else "")
            address = st.text_area("Address", value=editing.address if editing else "", height=60)
            latitude = st.number_input("Latitude", min_value=-90.0, max_value=90.0, value=float(editing.latitude if editing else 0.0), format="%.6f")
            longitude = st.number_input("Longitude", min_value=-180.0, max_value=180.0, value=float(editing.longitude if editing else 0.0), format="%.6f")
            operations_text = ""
            if editing:
                operations_text = ", ".join(editing.operations_list())
            operations_input = st.text_input("Operations (comma separated)", value=operations_text)
            default_components = []
            if editing:
                default_components = [
                    name for name, component_id in component_options.items() if any(link.component_id == component_id for link in editing.components)
                ]
            selected_components = st.multiselect("Components handled", list(component_options.keys()), default=default_components)
            submitted = st.form_submit_button("Save facility")
            if submitted:
                if not name.strip():
                    st.warning("Facility name is required.")
                    return
                ops_list = [entry.strip() for entry in operations_input.split(",") if entry.strip()]
                if editing:
                    facility = editing
                    facility.name = name.strip()
                    facility.facility_type = facility_type
                    facility.city = city.strip()
                    facility.country = country.strip()
                    facility.address = address.strip()
                    facility.latitude = latitude
                    facility.longitude = longitude
                    facility.set_operations(ops_list)
                else:
                    facility = Facility(
                        name=name.strip(),
                        facility_type=facility_type,
                        city=city.strip(),
                        country=country.strip(),
                        address=address.strip(),
                        latitude=latitude,
                        longitude=longitude,
                    )
                    facility.set_operations(ops_list)
                    session.add(facility)
                    session.flush()
                ensure_supply_node_for_facility(session, facility)

                selected_component_ids = {component_options[label] for label in selected_components}
                existing_links = {link.component_id: link for link in facility.components}
                for component_id in selected_component_ids:
                    if component_id not in existing_links:
                        session.add(FacilityComponent(facility_id=facility.id, component_id=component_id, operation=""))
                for component_id, link in list(existing_links.items()):
                    if component_id not in selected_component_ids:
                        session.delete(link)

                session.commit()
                st.success("Facility saved.")
                st.experimental_rerun()


def render_flow_form(session: Session) -> None:
    flows = session.execute(select(MaterialFlow)).scalars().all()
    flow_map = {
        f"{flow.from_node.name if flow.from_node else '?'} → {flow.to_node.name if flow.to_node else '?'} ({flow.component.name if flow.component else '—'})": flow
        for flow in flows
    }
    options = ["New flow"] + list(flow_map.keys())

    nodes = session.execute(select(SupplyNode).order_by(SupplyNode.name)).scalars().all()
    node_options = {f"{node.name} ({node.node_type})": node.id for node in nodes}

    components = session.execute(select(Component).order_by(Component.name)).scalars().all()
    component_options = {"— None —": None}
    component_options.update({component.name: component.id for component in components})

    with st.sidebar.expander("Add / Edit Material Flow"):
        selected_label = st.selectbox("Select flow", options, key="flow_select")
        editing = flow_map.get(selected_label)

        default_from = list(node_options.keys())[0] if node_options else ""
        default_to = list(node_options.keys())[0] if node_options else ""
        default_component = "— None —"
        if editing:
            for label, node_id in node_options.items():
                if node_id == editing.from_node_id:
                    default_from = label
                if node_id == editing.to_node_id:
                    default_to = label
            for label, component_id in component_options.items():
                if component_id == (editing.component_id or None):
                    default_component = label
                    break

        with st.form("flow_form"):
            if not node_options:
                st.info("Please create suppliers or facilities first.")
                st.form_submit_button("Save flow", disabled=True)
                return
            from_label = st.selectbox("From", list(node_options.keys()), index=list(node_options.keys()).index(default_from))
            to_label = st.selectbox("To", list(node_options.keys()), index=list(node_options.keys()).index(default_to))
            component_label = st.selectbox("Component (optional)", list(component_options.keys()), index=list(component_options.keys()).index(default_component))
            flow_type = st.selectbox("Flow type", ["component", "semi_finished", "finished"], index=0 if not editing else ["component", "semi_finished", "finished"].index(editing.flow_type if editing.flow_type in {"component", "semi_finished", "finished"} else "component"))
            lead_time = st.number_input("Lead time (days)", min_value=0.0, value=float(editing.lead_time_days if editing and editing.lead_time_days else 0.0), step=1.0)
            incoterms = st.text_input("Incoterms", value=editing.incoterms if editing else "")
            notes = st.text_area("Notes", value=editing.notes if editing else "", height=60)
            save = st.form_submit_button("Save flow")
            delete = st.form_submit_button("Delete flow", type="secondary") if editing else False

            if save:
                if from_label == to_label:
                    st.warning("From and To nodes must be different.")
                    return
                from_node_id = node_options[from_label]
                to_node_id = node_options[to_label]
                component_id = component_options[component_label]
                if editing:
                    flow = editing
                    flow.from_node_id = from_node_id
                    flow.to_node_id = to_node_id
                    flow.component_id = component_id
                    flow.flow_type = flow_type
                    flow.lead_time_days = lead_time or None
                    flow.incoterms = incoterms.strip()
                    flow.notes = notes.strip()
                else:
                    flow = MaterialFlow(
                        from_node_id=from_node_id,
                        to_node_id=to_node_id,
                        component_id=component_id,
                        flow_type=flow_type,
                        lead_time_days=lead_time or None,
                        incoterms=incoterms.strip(),
                        notes=notes.strip(),
                    )
                    session.add(flow)
                session.commit()
                st.success("Flow saved.")
                st.experimental_rerun()
            if delete and editing:
                session.delete(editing)
                session.commit()
                st.success("Flow deleted.")
                st.experimental_rerun()


# ------------------------------------------------------------------------------
# Import / export helpers
# ------------------------------------------------------------------------------

def export_tables_to_csv(session: Session) -> bytes:
    buffers: Dict[str, str] = {}

    facilities_rows = []
    for facility in session.execute(select(Facility).order_by(Facility.name)).scalars():
        facilities_rows.append(
            {
                "name": facility.name,
                "type": facility.facility_type,
                "address": facility.address,
                "city": facility.city,
                "country": facility.country,
                "lat": facility.latitude,
                "lon": facility.longitude,
                "operations": "; ".join(facility.operations_list()),
            }
        )
    buffers["facilities.csv"] = pd.DataFrame(facilities_rows).to_csv(index=False)

    products_rows = []
    for product in session.execute(select(Product).order_by(Product.name)).scalars():
        products_rows.append(
            {
                "name": product.name,
                "client_code": product.client_code or "",
                "description": product.description or "",
                "lifecycle_status": product.lifecycle_status or "",
            }
        )
    buffers["products.csv"] = pd.DataFrame(products_rows).to_csv(index=False)

    components_rows = []
    for component in session.execute(select(Component).order_by(Component.name)).scalars():
        product = component.product
        components_rows.append(
            {
                "product_code": product.client_code or product.name,
                "component_name": component.name,
                "uom": component.uom,
                "qty_per_product": component.qty_per_product,
                "spec_ref": component.spec_ref,
            }
        )
    buffers["components.csv"] = pd.DataFrame(components_rows).to_csv(index=False)

    suppliers_rows = []
    for supplier in session.execute(select(Supplier).order_by(Supplier.name)).scalars():
        suppliers_rows.append(
            {
                "name": supplier.name,
                "tier": supplier.tier,
                "address": supplier.address,
                "city": supplier.city,
                "country": supplier.country,
                "lat": supplier.latitude,
                "lon": supplier.longitude,
                "contact": supplier.primary_contact,
                "email": supplier.email,
                "phone": supplier.phone,
            }
        )
    buffers["suppliers.csv"] = pd.DataFrame(suppliers_rows).to_csv(index=False)

    supplier_components_rows = []
    for link in session.execute(select(SupplierComponent)).scalars():
        supplier = link.supplier
        component = link.component
        product = component.product if component else None
        supplier_components_rows.append(
            {
                "supplier_name": supplier.name if supplier else "",
                "product_code": product.client_code if product and product.client_code else (product.name if product else ""),
                "component_name": component.name if component else "",
                "capacity_per_month": link.capacity_units_per_month or "",
                "lead_time_days": link.lead_time_days or "",
                "moq": link.moq or "",
            }
        )
    buffers["supplier_components.csv"] = pd.DataFrame(supplier_components_rows).to_csv(index=False)

    flows_rows = []
    for flow in session.execute(select(MaterialFlow)).scalars():
        source = flow.from_node
        target = flow.to_node
        flows_rows.append(
            {
                "from_name": source.name if source else "",
                "from_type": source.node_type if source else "",
                "to_name": target.name if target else "",
                "to_type": target.node_type if target else "",
                "component_name": flow.component.name if flow.component else "",
                "flow_type": flow.flow_type,
                "lead_time_days": flow.lead_time_days or "",
                "incoterms": flow.incoterms,
                "notes": flow.notes,
            }
        )
    buffers["flows.csv"] = pd.DataFrame(flows_rows).to_csv(index=False)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, csv_text in buffers.items():
            zf.writestr(filename, csv_text)
    archive.seek(0)
    return archive.read()


def export_to_json(session: Session) -> bytes:
    export_payload = {
        "products": [
            {
                "name": product.name,
                "client_code": product.client_code,
                "description": product.description,
                "lifecycle_status": product.lifecycle_status,
            }
            for product in session.execute(select(Product)).scalars()
        ],
        "components": [
            {
                "name": component.name,
                "product": component.product.client_code or component.product.name,
                "uom": component.uom,
                "qty_per_product": component.qty_per_product,
            }
            for component in session.execute(select(Component)).scalars()
        ],
        "suppliers": [
            {
                "name": supplier.name,
                "tier": supplier.tier,
                "city": supplier.city,
                "country": supplier.country,
                "lat": supplier.latitude,
                "lon": supplier.longitude,
            }
            for supplier in session.execute(select(Supplier)).scalars()
        ],
        "facilities": [
            {
                "name": facility.name,
                "type": facility.facility_type,
                "city": facility.city,
                "country": facility.country,
                "lat": facility.latitude,
                "lon": facility.longitude,
            }
            for facility in session.execute(select(Facility)).scalars()
        ],
        "flows": [
            {
                "from": {"name": flow.from_node.name if flow.from_node else "", "type": flow.from_node.node_type if flow.from_node else ""},
                "to": {"name": flow.to_node.name if flow.to_node else "", "type": flow.to_node.node_type if flow.to_node else ""},
                "component": flow.component.name if flow.component else None,
                "flow_type": flow.flow_type,
                "lead_time_days": flow.lead_time_days,
                "incoterms": flow.incoterms,
                "notes": flow.notes,
            }
            for flow in session.execute(select(MaterialFlow)).scalars()
        ],
    }
    return json.dumps(export_payload, indent=2).encode("utf-8")


def parse_operations_field(raw: str) -> List[str]:
    if not raw:
        return []
    if raw.strip().startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in raw.split(";") if item.strip()]


def import_csv_bundle(session: Session, archive_bytes: bytes) -> List[str]:
    messages: List[str] = []
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        namelist = set(zf.namelist())

        if "products.csv" in namelist:
            df_products = pd.read_csv(zf.open("products.csv"))
            for _, row in df_products.iterrows():
                product = session.execute(select(Product).where(Product.name == row["name"])).scalar_one_or_none()
                if product is None:
                    product = Product(name=row["name"])
                    session.add(product)
                product.client_code = str(row.get("client_code", "") or "") or None
                product.description = str(row.get("description", "") or "")
                product.lifecycle_status = str(row.get("lifecycle_status", "") or "active")
                messages.append(f"Upserted product {product.name}")

        if "components.csv" in namelist:
            df_components = pd.read_csv(zf.open("components.csv"))
            for _, row in df_components.iterrows():
                product_code = str(row.get("product_code", "") or "")
                product = session.execute(
                    select(Product).where((Product.client_code == product_code) | (Product.name == product_code))
                ).scalar_one_or_none()
                if product is None:
                    product = Product(name=product_code or f"Imported Product {uuid_str()[:8]}", client_code=product_code or None)
                    session.add(product)
                    session.flush()
                    messages.append(f"Created placeholder product {product.name}")
                component = session.execute(select(Component).where(Component.name == row["component_name"])).scalar_one_or_none()
                if component is None:
                    component = Component(product_id=product.id, name=row["component_name"])
                    session.add(component)
                component.product_id = product.id
                component.uom = str(row.get("uom", "") or "")
                component.qty_per_product = float(row.get("qty_per_product", 0) or 0)
                component.spec_ref = str(row.get("spec_ref", "") or "")
                messages.append(f"Upserted component {component.name}")

        if "facilities.csv" in namelist:
            df_facilities = pd.read_csv(zf.open("facilities.csv"))
            for _, row in df_facilities.iterrows():
                facility = session.execute(select(Facility).where(Facility.name == row["name"])).scalar_one_or_none()
                if facility is None:
                    facility = Facility(
                        name=row["name"],
                        facility_type=str(row.get("type", "assembly")).lower(),
                        latitude=float(row.get("lat", 0.0) or 0.0),
                        longitude=float(row.get("lon", 0.0) or 0.0),
                    )
                    session.add(facility)
                facility.facility_type = str(row.get("type", "assembly")).lower()
                facility.address = str(row.get("address", "") or "")
                facility.city = str(row.get("city", "") or "")
                facility.country = str(row.get("country", "") or "")
                facility.latitude = float(row.get("lat", 0.0) or 0.0)
                facility.longitude = float(row.get("lon", 0.0) or 0.0)
                facility.set_operations(parse_operations_field(str(row.get("operations", "") or "")))
                ensure_supply_node_for_facility(session, facility)
                messages.append(f"Upserted facility {facility.name}")

        if "suppliers.csv" in namelist:
            df_suppliers = pd.read_csv(zf.open("suppliers.csv"))
            for _, row in df_suppliers.iterrows():
                supplier = session.execute(select(Supplier).where(Supplier.name == row["name"])).scalar_one_or_none()
                if supplier is None:
                    supplier = Supplier(
                        name=row["name"],
                        tier=int(row.get("tier", 1) or 1),
                        latitude=float(row.get("lat", 0.0) or 0.0),
                        longitude=float(row.get("lon", 0.0) or 0.0),
                    )
                    session.add(supplier)
                supplier.tier = int(row.get("tier", supplier.tier or 1) or 1)
                supplier.address = str(row.get("address", "") or "")
                supplier.city = str(row.get("city", "") or "")
                supplier.country = str(row.get("country", "") or "")
                supplier.latitude = float(row.get("lat", supplier.latitude or 0.0) or 0.0)
                supplier.longitude = float(row.get("lon", supplier.longitude or 0.0) or 0.0)
                supplier.primary_contact = str(row.get("contact", "") or "")
                supplier.email = str(row.get("email", "") or "")
                supplier.phone = str(row.get("phone", "") or "")
                ensure_supply_node_for_supplier(session, supplier)
                messages.append(f"Upserted supplier {supplier.name}")

        if "supplier_components.csv" in namelist:
            df_sc = pd.read_csv(zf.open("supplier_components.csv"))
            for _, row in df_sc.iterrows():
                supplier = session.execute(select(Supplier).where(Supplier.name == row["supplier_name"])).scalar_one_or_none()
                component = session.execute(select(Component).where(Component.name == row["component_name"])).scalar_one_or_none()
                if supplier is None or component is None:
                    continue
                link = session.execute(
                    select(SupplierComponent).where(
                        SupplierComponent.supplier_id == supplier.id,
                        SupplierComponent.component_id == component.id,
                    )
                ).scalar_one_or_none()
                if link is None:
                    link = SupplierComponent(supplier_id=supplier.id, component_id=component.id)
                    session.add(link)
                link.capacity_units_per_month = float(row.get("capacity_per_month", 0) or 0) or None
                link.lead_time_days = float(row.get("lead_time_days", 0) or 0) or None
                link.moq = float(row.get("moq", 0) or 0) or None
                messages.append(f"Linked supplier {supplier.name} to component {component.name}")

        if "flows.csv" in namelist:
            df_flows = pd.read_csv(zf.open("flows.csv"))
            for _, row in df_flows.iterrows():
                source = session.execute(
                    select(SupplyNode).where(SupplyNode.name == row["from_name"], SupplyNode.node_type == row["from_type"])
                ).scalar_one_or_none()
                target = session.execute(
                    select(SupplyNode).where(SupplyNode.name == row["to_name"], SupplyNode.node_type == row["to_type"])
                ).scalar_one_or_none()
                component = session.execute(select(Component).where(Component.name == row["component_name"])).scalar_one_or_none()
                if source is None or target is None:
                    continue
                flow = MaterialFlow(
                    from_node_id=source.id,
                    to_node_id=target.id,
                    component_id=component.id if component else None,
                    flow_type=row.get("flow_type", "component"),
                    lead_time_days=float(row.get("lead_time_days", 0) or 0) or None,
                    incoterms=str(row.get("incoterms", "") or ""),
                    notes=str(row.get("notes", "") or ""),
                )
                session.add(flow)
                messages.append(f"Added flow {source.name} → {target.name}")

    session.commit()
    return messages


def resolve_node(session: Session, payload: Dict[str, str]) -> Optional[SupplyNode]:
    name = payload.get("name")
    node_type = payload.get("type")
    if not name or not node_type:
        return None
    return session.execute(
        select(SupplyNode).where(SupplyNode.name == name, SupplyNode.node_type == node_type)
    ).scalar_one_or_none()


def import_json_flows(session: Session, uploaded_file) -> int:
    if hasattr(uploaded_file, "read"):
        data = json.load(uploaded_file)
    else:
        data = json.loads(uploaded_file)
    flows_payload = data if isinstance(data, list) else data.get("flows", [])
    count = 0
    for entry in flows_payload:
        source = resolve_node(session, entry.get("from", {}))
        target = resolve_node(session, entry.get("to", {}))
        if source is None or target is None:
            continue
        component_name = entry.get("component")
        component = None
        if component_name:
            component = session.execute(select(Component).where(Component.name == component_name)).scalar_one_or_none()
        flow = MaterialFlow(
            from_node_id=source.id,
            to_node_id=target.id,
            component_id=component.id if component else None,
            flow_type=entry.get("flow_type", "component"),
            lead_time_days=entry.get("lead_time_days"),
            incoterms=entry.get("incoterms", ""),
            notes=entry.get("notes", ""),
        )
        session.add(flow)
        count += 1
    session.commit()
    return count


def render_import_export(session: Session) -> None:
    with st.sidebar.expander("Import / Export"):
        csv_bytes = export_tables_to_csv(session)
        st.download_button(
            "Export CSV bundle",
            data=csv_bytes,
            file_name="supply_network_export.zip",
            mime="application/zip",
        )
        json_bytes = export_to_json(session)
        st.download_button(
            "Export JSON snapshot",
            data=json_bytes,
            file_name="supply_network_export.json",
            mime="application/json",
        )

        csv_upload = st.file_uploader("Import CSV bundle (.zip)", type="zip", key="csv_import")
        if csv_upload is not None and st.button("Process CSV Import", key="csv_import_button"):
            try:
                messages = import_csv_bundle(session, csv_upload.getvalue())
            except Exception as exc:
                st.error(f"CSV import failed: {exc}")
            else:
                st.success(f"Import complete. {len(messages)} items processed.")
                for message in messages[:10]:
                    st.write(f"• {message}")
                if len(messages) > 10:
                    st.write(f"... {len(messages) - 10} more")
                st.experimental_rerun()

        json_upload = st.file_uploader("Import flows (.json)", type="json", key="json_import")
        if json_upload is not None and st.button("Process JSON Import", key="json_import_button"):
            try:
                count = import_json_flows(session, io.BytesIO(json_upload.getvalue()))
            except Exception as exc:
                st.error(f"JSON import failed: {exc}")
            else:
                st.success(f"Imported {count} flows.")
                st.experimental_rerun()


# ------------------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Supply Chain Mapping Prototype", layout="wide", page_icon="🌐")
    st.title("Supply Chain Mapping Prototype")
    st.caption("Manage Maple Grid Systems' transformer network, suppliers, and flows.")

    with SessionFactory() as session:
        filters = render_filters(session)
        map_data = collect_visual_data(session, filters)
        if map_data.missing_coordinates:
            st.warning(f"Missing coordinates for: {', '.join(map_data.missing_coordinates)}")

        map_col, summary_col = st.columns([3.0, 1.2])
        with map_col:
            st.subheader("Network Map")
            render_map(map_data)
        with summary_col:
            render_summary(session, filters, map_data)

        st.divider()
        render_import_export(session)
        st.sidebar.divider()
        render_crud_forms(session)


if __name__ == "__main__":
    main()
