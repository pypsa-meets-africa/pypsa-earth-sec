import os
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
from helpers import create_dummy_data
from helpers import create_network_topology
from helpers import mock_snakemake
from helpers import prepare_costs
import pytz
import xarray as xr
spatial = SimpleNamespace()


def generate_periodic_profiles(dt_index, nodes, weekly_profile, localize=None):
    """
    Give a 24*7 long list of weekly hourly profiles, generate this for each
    country for the period dt_index, taking account of time zones and summer time.
    """

    weekly_profile = pd.Series(weekly_profile, range(24*7))

    week_df = pd.DataFrame(index=dt_index, columns=nodes)

    for node in nodes:
        timezone = pytz.timezone(pytz.country_timezones[node[:2]][0])
        tz_dt_index = dt_index.tz_convert(timezone)
        week_df[node] = [24 * dt.weekday() + dt.hour for dt in tz_dt_index]
        week_df[node] = week_df[node].map(weekly_profile)

    week_df = week_df.tz_localize(localize)

    return week_df


def transport_degree_factor(
        temperature,
        deadband_lower=15,
        deadband_upper=20,
        lower_degree_factor=0.5,
        upper_degree_factor=1.6):
    """
    Work out how much energy demand in vehicles increases due to heating and cooling.
    There is a deadband where there is no increase.
    Degree factors are % increase in demand compared to no heating/cooling fuel consumption.
    Returns per unit increase in demand for each place and time
    """

    dd = temperature.copy()

    dd[(temperature > deadband_lower) & (temperature < deadband_upper)] = 0.

    dT_lower = deadband_lower - temperature[temperature < deadband_lower]
    dd[temperature < deadband_lower] = lower_degree_factor / 100 * dT_lower

    dT_upper = temperature[temperature > deadband_upper] - deadband_upper
    dd[temperature > deadband_upper] = upper_degree_factor / 100 * dT_upper

    return dd


# TODO separate sectors and move into own rules
def prepare_transport_data(n):

    energy_totals = pd.read_csv(
        snakemake.input.energy_totals_name, index_col=0)

    nodal_energy_totals = energy_totals.loc[n.buses.country.dropna()].fillna(
        0.)
    nodal_energy_totals.index = nodes
    # district heat share not weighted by population
    district_heat_share = nodal_energy_totals["district heat share"].round(2)
    nodal_energy_totals = nodal_energy_totals.multiply(
        0.25, axis=0)  # TODO change 0.25 by pop_layout.fraction

    ##############
    # Transport
    ##############

    # Get overall demand curve for all vehicles

    traffic = pd.read_csv(snakemake.input.traffic_data_KFZ,
                          skiprows=2, usecols=["count"], squeeze=True)

    # Generate profiles
    transport_shape = generate_periodic_profiles(
        dt_index=n.snapshots.tz_localize("UTC"),
        nodes=nodes,
        weekly_profile=traffic.values
    )
    transport_shape = transport_shape / transport_shape.sum()

    transport_data = pd.read_csv(snakemake.input.transport_name, index_col=0)

    nodal_transport_data = transport_data.loc[n.buses.country.dropna()].fillna(
        0.)
    nodal_transport_data.index = nodes
    # TODO pop_layout["fraction"] instead of 0.25
    nodal_transport_data["number cars"] = 0.25 * \
        nodal_transport_data["number cars"]
    nodal_transport_data.loc[nodal_transport_data["average fuel efficiency"] == 0.,
                             "average fuel efficiency"] = transport_data["average fuel efficiency"].mean()

    # electric motors are more efficient, so alter transport demand

    plug_to_wheels_eta = options.get("bev_plug_to_wheel_efficiency", 0.2)
    battery_to_wheels_eta = plug_to_wheels_eta * \
        options.get("bev_charge_efficiency", 0.9)

    efficiency_gain = nodal_transport_data["average fuel efficiency"] / \
        battery_to_wheels_eta

    # get heating demand for correction to demand time series
    temperature = xr.open_dataarray(snakemake.input.temp_air_total).to_pandas()

    # correction factors for vehicle heating
    dd_ICE = transport_degree_factor(
        temperature,
        options['transport_heating_deadband_lower'],
        options['transport_heating_deadband_upper'],
        options['ICE_lower_degree_factor'],
        options['ICE_upper_degree_factor']
    )

    dd_EV = transport_degree_factor(
        temperature,
        options['transport_heating_deadband_lower'],
        options['transport_heating_deadband_upper'],
        options['EV_lower_degree_factor'],
        options['EV_upper_degree_factor']
    )

    # divide out the heating/cooling demand from ICE totals
    # and multiply back in the heating/cooling demand for EVs
    ice_correction = (transport_shape * (1 + dd_ICE)
                      ).sum() / transport_shape.sum()

    energy_totals_transport = nodal_energy_totals["total road"] + \
        nodal_energy_totals["total rail"] - \
        nodal_energy_totals["electricity rail"]

    transport = (transport_shape.multiply(energy_totals_transport) * 1e6 *
                 Nyears).divide(efficiency_gain * ice_correction).multiply(1 + dd_EV)

    # derive plugged-in availability for PKW's (cars)

    traffic = pd.read_csv(snakemake.input.traffic_data_Pkw,
                          skiprows=2, usecols=["count"], squeeze=True)

    avail_max = options.get("bev_avail_max", 0.95)
    avail_mean = options.get("bev_avail_mean", 0.8)

    avail = avail_max - (avail_max - avail_mean) * (traffic -
                                                    traffic.min()) / (traffic.mean() - traffic.min())

    avail_profile = generate_periodic_profiles(
        dt_index=n.snapshots.tz_localize("UTC"),
        nodes=nodes,
        weekly_profile=avail.values
    )

    dsm_week = np.zeros((24*7,))

    dsm_week[(np.arange(0, 7, 1) * 24 + options['bev_dsm_restriction_time'])
             ] = options['bev_dsm_restriction_value']

    dsm_profile = generate_periodic_profiles(
        dt_index=n.snapshots.tz_localize("UTC"),
        nodes=nodes,
        weekly_profile=dsm_week
    )

    return nodal_energy_totals, transport, avail_profile, dsm_profile, nodal_transport_data, district_heat_share


def add_carrier_buses(n, carriers):
    """
    Add buses to connect e.g. coal, nuclear and oil plants
    """
    if isinstance(carriers, str):
        carriers = [carriers]

    for carrier in carriers:

        n.add("Carrier", carrier)

        n.add("Bus", "Africa " + carrier, location="Africa", carrier=carrier)

        # capital cost could be corrected to e.g. 0.2 EUR/kWh * annuity and O&M
        n.add(
            "Store",
            "Africa " + carrier + " Store",
            bus="Africa " + carrier,
            e_nom_extendable=True,
            e_cyclic=True,
            carrier=carrier,
        )

        n.add(
            "Generator",
            "Africa " + carrier,
            bus="Africa " + carrier,
            p_nom_extendable=True,
            carrier=carrier,
            marginal_cost=costs.at[carrier, "fuel"],
        )


def add_generation(n, costs):
    """
    Adds conventional generation as specified in config
    """

    print("adding electricity generation")

    # Not required, because nodes are already defined in "nodes"
    # nodes = pop_layout.index

    fallback = {"OCGT": "gas"}
    conventionals = options.get("conventional_generation", fallback)

    add_carrier_buses(n, np.unique(list(conventionals.values())))

    for generator, carrier in conventionals.items():

        n.madd(
            "Link",
            nodes + " " + generator,
            bus0="Africa " + carrier,
            bus1=nodes,
            bus2="co2 atmosphere",
            marginal_cost=costs.at[generator, "efficiency"] *
            costs.at[generator, "VOM"],  # NB: VOM is per MWel
            # NB: fixed cost is per MWel
            capital_cost=costs.at[generator, "efficiency"] *
            costs.at[generator, "fixed"],
            p_nom_extendable=True,
            carrier=generator,
            efficiency=costs.at[generator, "efficiency"],
            efficiency2=costs.at[carrier, "CO2 intensity"],
            lifetime=costs.at[generator, "lifetime"],
        )


def add_oil(n, costs):
    """
    Function to add oil carrier and bus to network. If-Statements are required in
    case oil was already added from config ['sector']['conventional_generation']
    Oil is copper plated
    """
    # TODO function will not be necessary if conventionals are added using "add_carrier_buses()"
    # TODO before using add_carrier_buses: remove_elec_base_techs(n), otherwise carriers are added double

    if "oil" not in n.carriers.index:
        n.add("Carrier", "oil")

    if "Africa oil" not in n.buses.index:

        n.add("Bus", "Africa oil", location="Africa", carrier="oil")

    if "Africa oil Store" not in n.stores.index:

        # could correct to e.g. 0.001 EUR/kWh * annuity and O&M
        n.add(
            "Store",
            "Africa oil Store",
            bus="Africa oil",
            e_nom_extendable=True,
            e_cyclic=True,
            carrier="oil",
        )

    if "Africa oil" not in n.generators.index:

        n.add(
            "Generator",
            "Africa oil",
            bus="Africa oil",
            p_nom_extendable=True,
            carrier="oil",
            marginal_cost=costs.at["oil", "fuel"],
        )


def H2_liquid_fossil_conversions(n, costs):
    """
    Function to add conversions between H2 and liquid fossil
    Carrier and bus is added in add_oil, which later on might be switched to add_generation
    """

    n.madd(
        "Link",
        nodes + " Fischer-Tropsch",
        bus0=nodes + " H2",
        bus1="Africa oil",
        bus2=spatial.co2.nodes,
        carrier="Fischer-Tropsch",
        efficiency=costs.at["Fischer-Tropsch", "efficiency"],
        capital_cost=costs.at["Fischer-Tropsch", "fixed"],
        efficiency2=-costs.at["oil", "CO2 intensity"] *
        costs.at["Fischer-Tropsch", "efficiency"],
        p_nom_extendable=True,
        lifetime=costs.at["Fischer-Tropsch", "lifetime"],
    )


def add_hydrogen(n, costs):
    "function to add hydrogen as an energy carrier with its conversion technologies from and to AC"

    n.add("Carrier", "H2")

    n.madd("Bus", nodes + " H2", location=nodes, carrier="H2")

    n.madd(
        "Link",
        nodes + " H2 Electrolysis",
        bus1=nodes + " H2",
        bus0=nodes,
        p_nom_extendable=True,
        carrier="H2 Electrolysis",
        efficiency=costs.at["electrolysis", "efficiency"],
        capital_cost=costs.at["electrolysis", "fixed"],
        lifetime=costs.at["electrolysis", "lifetime"],
    )

    n.madd(
        "Link",
        nodes + " H2 Fuel Cell",
        bus0=nodes + " H2",
        bus1=nodes,
        p_nom_extendable=True,
        carrier="H2 Fuel Cell",
        efficiency=costs.at["fuel cell", "efficiency"],
        # NB: fixed cost is per MWel
        capital_cost=costs.at["fuel cell", "fixed"] *
        costs.at["fuel cell", "efficiency"],
        lifetime=costs.at["fuel cell", "lifetime"],
    )

    cavern_nodes = pd.DataFrame()
    if options["hydrogen_underground_storage"]:

        h2_salt_cavern_potential = pd.read_csv(snakemake.input.h2_cavern,
                                               index_col=0,
                                               squeeze=True)
        h2_cavern_ct = h2_salt_cavern_potential[~h2_salt_cavern_potential.isna(
        )]
        cavern_nodes = n.buses[n.buses.country.isin(h2_cavern_ct.index)]

        h2_capital_cost = costs.at["hydrogen storage underground", "fixed"]

        # assumptions: weight storage potential in a country by population
        # TODO: fix with real geographic potentials
        # convert TWh to MWh with 1e6
        h2_pot = h2_cavern_ct.loc[cavern_nodes.country]
        h2_pot.index = cavern_nodes.index
        # h2_pot = h2_pot * cavern_nodes.fraction * 1e6

        n.madd(
            "Store",
            cavern_nodes.index + " H2 Store",
            bus=cavern_nodes.index + " H2",
            e_nom_extendable=True,
            e_nom_max=h2_pot.values,
            e_cyclic=True,
            carrier="H2 Store",
            capital_cost=h2_capital_cost,
        )

    # hydrogen stored overground (where not already underground)
    h2_capital_cost = costs.at["hydrogen storage tank incl. compressor",
                               "fixed"]
    nodes_overground = cavern_nodes.index.symmetric_difference(nodes)

    n.madd(
        "Store",
        nodes_overground + " H2 Store",
        bus=nodes_overground + " H2",
        e_nom_extendable=True,
        e_cyclic=True,
        carrier="H2 Store",
        capital_cost=h2_capital_cost,
    )

    attrs = ["bus0", "bus1", "length"]
    h2_links = pd.DataFrame(columns=attrs)

    candidates = pd.concat({
        "lines": n.lines[attrs],
        "links": n.links.loc[n.links.carrier == "DC", attrs]
    })

    for candidate in candidates.index:
        buses = [
            candidates.at[candidate, "bus0"], candidates.at[candidate, "bus1"]
        ]
        buses.sort()
        name = f"H2 pipeline {buses[0]} -> {buses[1]}"
        if name not in h2_links.index:
            h2_links.at[name, "bus0"] = buses[0]
            h2_links.at[name, "bus1"] = buses[1]
            h2_links.at[name, "length"] = candidates.at[candidate, "length"]

    # TODO Add efficiency losses
    n.madd(
        "Link",
        h2_links.index,
        bus0=h2_links.bus0.values + " H2",
        bus1=h2_links.bus1.values + " H2",
        p_min_pu=-1,
        p_nom_extendable=True,
        length=h2_links.length.values,
        capital_cost=costs.at["H2 (g) pipeline", "fixed"] *
        h2_links.length.values,
        carrier="H2 pipeline",
        lifetime=costs.at["H2 (g) pipeline", "lifetime"],
    )


def add_co2(n, costs):
    "add carbon carrier, it's networks and storage units"
    spatial.nodes = nodes

    spatial.co2 = SimpleNamespace()

    if options["co2_network"]:
        spatial.co2.nodes = nodes + " co2 stored"
        spatial.co2.locations = nodes
        spatial.co2.vents = nodes + " co2 vent"
    else:
        spatial.co2.nodes = ["co2 stored"]
        spatial.co2.locations = ["Africa"]
        spatial.co2.vents = ["co2 vent"]

    spatial.co2.df = pd.DataFrame(vars(spatial.co2), index=nodes)

    # minus sign because opposite to how fossil fuels used:
    # CH4 burning puts CH4 down, atmosphere up
    n.add("Carrier", "co2", co2_emissions=-1.0)

    # this tracks CO2 in the atmosphere
    n.add(
        "Bus",
        "co2 atmosphere",
        location="Africa",  # TODO Ignoed by pypsa chck
        carrier="co2",
    )

    # can also be negative
    n.add(
        "Store",
        "co2 atmosphere",
        e_nom_extendable=True,
        e_min_pu=-1,
        carrier="co2",
        bus="co2 atmosphere",
    )

    # this tracks CO2 stored, e.g. underground
    n.madd("Bus",
           spatial.co2.nodes,
           location=spatial.co2.locations,
           carrier="co2 stored")

    n.madd(
        "Store",
        spatial.co2.nodes.str[:-2] + "age",
        e_nom_extendable=True,
        e_nom_max=np.inf,
        capital_cost=options["co2_sequestration_cost"],
        carrier="co2 stored",
        bus=spatial.co2.nodes,
    )

    n.madd(
        "Link",
        spatial.co2.vents,
        bus0=spatial.co2.nodes,
        bus1="co2 atmosphere",
        carrier="co2 vent",
        efficiency=1.0,
        p_nom_extendable=True,
    )

    # logger.info("Adding CO2 network.")
    co2_links = create_network_topology(n, "CO2 pipeline ")

    cost_onshore = ((1 - co2_links.underwater_fraction) *
                    costs.at["CO2 pipeline", "fixed"] * co2_links.length)
    cost_submarine = (co2_links.underwater_fraction *
                      costs.at["CO2 submarine pipeline", "fixed"] *
                      co2_links.length)
    capital_cost = cost_onshore + cost_submarine

    n.madd(
        "Link",
        co2_links.index,
        bus0=co2_links.bus0.values + " co2 stored",
        bus1=co2_links.bus1.values + " co2 stored",
        p_min_pu=-1,
        p_nom_extendable=True,
        length=co2_links.length.values,
        capital_cost=capital_cost.values,
        carrier="CO2 pipeline",
        lifetime=costs.at["CO2 pipeline", "lifetime"],
    )

    n.madd(
        "Store",
        spatial.co2.nodes,
        e_nom_extendable=True,
        e_nom_max=np.inf,
        capital_cost=options["co2_sequestration_cost"],
        carrier="co2 stored",
        bus=spatial.co2.nodes,
    )

    # logger.info("Adding CO2 network.")
    co2_links = create_network_topology(n, "CO2 pipeline ")

    cost_onshore = ((1 - co2_links.underwater_fraction) *
                    costs.at["CO2 pipeline", "fixed"] * co2_links.length)
    cost_submarine = (co2_links.underwater_fraction *
                      costs.at["CO2 submarine pipeline", "fixed"] *
                      co2_links.length)
    capital_cost = cost_onshore + cost_submarine


# def add_aviation(n, cost):

#     all_aviation = ["total international aviation", "total domestic aviation"]
#     p_set = nodal_energy_totals.loc[nodes, all_aviation].sum(axis=1).sum() * 1e6 / 8760

#     n.add("Load",
#         "kerosene for aviation",
#         bus="EU oil",
#         carrier="kerosene for aviation",
#         p_set=p_set
#     )

#     co2_release = ["kerosene for aviation"]
#     co2 = n.loads.loc[co2_release, "p_set"].sum() * costs.at["oil", 'CO2 intensity'] / 8760

#     n.add("Load",
#         "oil emissions",
#         bus="co2 atmosphere",
#         carrier="oil emissions",
#         p_set=-co2
#     )


def add_storage(n, costs):
    "function to add the different types of storage systems"
    n.add("Carrier", "battery")

    n.madd("Bus", nodes + " battery", location=nodes, carrier="battery")

    n.madd(
        "Store",
        nodes + " battery",
        bus=nodes + " battery",
        e_cyclic=True,
        e_nom_extendable=True,
        carrier="battery",
        capital_cost=costs.at["battery storage", "fixed"],
        lifetime=costs.at["battery storage", "lifetime"],
    )

    n.madd(
        "Link",
        nodes + " battery charger",
        bus0=nodes,
        bus1=nodes + " battery",
        carrier="battery charger",
        efficiency=costs.at["battery inverter", "efficiency"]**0.5,
        capital_cost=costs.at["battery inverter", "fixed"],
        p_nom_extendable=True,
        lifetime=costs.at["battery inverter", "lifetime"],
    )

    n.madd(
        "Link",
        nodes + " battery discharger",
        bus0=nodes + " battery",
        bus1=nodes,
        carrier="battery discharger",
        efficiency=costs.at["battery inverter", "efficiency"]**0.5,
        marginal_cost=options["marginal_cost_storage"],
        p_nom_extendable=True,
        lifetime=costs.at["battery inverter", "lifetime"],
    )


def h2_hc_conversions(n, costs):
    "function to add the conversion technologies between H2 and hydrocarbons"
    if options["methanation"]:

        n.madd(
            "Link",
            spatial.nodes,
            suffix=" Sabatier",
            bus0=nodes + " H2",
            bus1="Africa gas",
            bus2=spatial.co2.nodes,
            p_nom_extendable=True,
            carrier="Sabatier",
            efficiency=costs.at["methanation", "efficiency"],
            efficiency2=-costs.at["methanation", "efficiency"] *
            costs.at["gas", "CO2 intensity"],
            # costs given per kW_gas
            capital_cost=costs.at["methanation", "fixed"] *
            costs.at["methanation", "efficiency"],
            lifetime=costs.at["methanation", "lifetime"],
        )

    if options["helmeth"]:

        n.madd(
            "Link",
            spatial.nodes,
            suffix=" helmeth",
            bus0=nodes,
            bus1="Africa gas",
            bus2=spatial.co2.nodes,
            carrier="helmeth",
            p_nom_extendable=True,
            efficiency=costs.at["helmeth", "efficiency"],
            efficiency2=-costs.at["helmeth", "efficiency"] *
            costs.at["gas", "CO2 intensity"],
            capital_cost=costs.at["helmeth", "fixed"],
            lifetime=costs.at["helmeth", "lifetime"],
        )

    if options["SMR"]:

        n.madd(
            "Link",
            spatial.nodes,
            suffix=" SMR CC",
            bus0="Africa gas",
            bus1=nodes + " H2",
            bus2="co2 atmosphere",
            bus3=spatial.co2.nodes,
            p_nom_extendable=True,
            carrier="SMR CC",
            efficiency=costs.at["SMR CC", "efficiency"],
            efficiency2=costs.at["gas", "CO2 intensity"] *
            (1 - options["cc_fraction"]),
            efficiency3=costs.at["gas", "CO2 intensity"] *
            options["cc_fraction"],
            capital_cost=costs.at["SMR CC", "fixed"],
            lifetime=costs.at["SMR CC", "lifetime"],
        )

        n.madd(
            "Link",
            nodes + " SMR",
            bus0="Africa gas",
            bus1=nodes + " H2",
            bus2="co2 atmosphere",
            p_nom_extendable=True,
            carrier="SMR",
            efficiency=costs.at["SMR", "efficiency"],
            efficiency2=costs.at["gas", "CO2 intensity"],
            capital_cost=costs.at["SMR", "fixed"],
            lifetime=costs.at["SMR", "lifetime"],
        )


def add_industry(n, costs):

    #     print("adding industrial demand")

    #     # 1e6 to convert TWh to MWh
    #     industrial_demand = pd.read_csv(snakemake.input.industrial_demand, index_col=0) * 1e6
    industrial_demand = create_dummy_data(n, "industry", "")

    # TODO carrier Biomass

    # CARRIER = FOSSIL GAS

    n.add("Bus", "gas for industry", location="EU", carrier="gas for industry")

    n.add(
        "Load",
        "gas for industry",
        bus="gas for industry",
        carrier="gas for industry",
        p_set=industrial_demand.loc[nodes, "methane"].sum() / 8760,
    )

    n.add(
        "Link",
        "gas for industry",
        bus0="Africa gas",
        bus1="gas for industry",
        bus2="co2 atmosphere",
        carrier="gas for industry",
        p_nom_extendable=True,
        efficiency=1.0,
        efficiency2=costs.at["gas", "CO2 intensity"],
    )

    n.madd(
        "Link",
        spatial.co2.locations,
        suffix=" gas for industry CC",
        bus0="Africa gas",
        bus1="gas for industry",
        bus2="co2 atmosphere",
        bus3=spatial.co2.nodes,
        carrier="gas for industry CC",
        p_nom_extendable=True,
        capital_cost=costs.at["cement capture", "fixed"] *
        costs.at["gas", "CO2 intensity"],
        efficiency=0.9,
        efficiency2=costs.at["gas", "CO2 intensity"] *
        (1 - costs.at["cement capture", "capture_rate"]),
        efficiency3=costs.at["gas", "CO2 intensity"] *
        costs.at["cement capture", "capture_rate"],
        lifetime=costs.at["cement capture", "lifetime"],
    )

    #################################################### CARRIER = HYDROGEN
    n.madd(
        "Load",
        nodes,
        suffix=" H2 for industry",
        bus=nodes + " H2",
        carrier="H2 for industry",
        p_set=industrial_demand.loc[nodes, "hydrogen"] / 8760,
    )

    # CARRIER = LIQUID HYDROCARBONS
    n.add(
        "Load",
        "naphtha for industry",
        bus="Africa oil",
        carrier="naphtha for industry",
        p_set=industrial_demand.loc[nodes, "naphtha"].sum() / 8760,
    )

    #     #NB: CO2 gets released again to atmosphere when plastics decay or kerosene is burned
    #     #except for the process emissions when naphtha is used for petrochemicals, which can be captured with other industry process emissions
    #     #tco2 per hour
    # TODO kerosene for aviation should be added too but in the right func.
    co2_release = ["naphtha for industry"]
    # check land tranport
    co2 = (
        n.loads.loc[co2_release, "p_set"].sum() *
        costs.at["oil", "CO2 intensity"] -
        industrial_demand.loc[nodes, "process emission from feedstock"].sum() /
        8760)

    n.add(
        "Load",
        "industry oil emissions",
        bus="co2 atmosphere",
        carrier="industry oil emissions",
        p_set=-co2,
    )

    ########################################################### CARIER = HEAT
    #     # TODO simplify bus expression
    #     n.madd("Load",
    #         nodes,
    #         suffix=" low-temperature heat for industry",
    #         bus=[node + " urban central heat" if node + " urban central heat" in n.buses.index else node + " services urban decentral heat" for node in nodes],
    #         carrier="low-temperature heat for industry",
    #         p_set=industrial_demand.loc[nodes, "low-temperature heat"] / 8760
    #     )

    ################################################## CARRIER = ELECTRICITY

    #     # remove today's industrial electricity demand by scaling down total electricity demand
    for ct in n.buses.country.dropna().unique():
        # TODO map onto n.bus.country
        # TODO make sure to check this one, should AC have carrier pf "electricity"?
        loads_i = n.loads.index[(n.loads.index.str[:2] == ct)
                                & (n.loads.carrier == "electricity")]
        if n.loads_t.p_set[loads_i].empty:
            continue
        factor = (1 -
                  industrial_demand.loc[loads_i, "current electricity"].sum() /
                  n.loads_t.p_set[loads_i].sum().sum())
        n.loads_t.p_set[loads_i] *= factor

    n.madd(
        "Load",
        nodes,
        suffix=" industry electricity",
        bus=nodes,
        carrier="industry electricity",
        p_set=industrial_demand.loc[nodes, "electricity"] / 8760,
    )

    n.add("Bus",
          "process emissions",
          location="EU",
          carrier="process emissions")

    # this should be process emissions fossil+feedstock
    # then need load on atmosphere for feedstock emissions that are currently going to atmosphere via Link Fischer-Tropsch demand
    n.add(
        "Load",
        "process emissions",
        bus="process emissions",
        carrier="process emissions",
        p_set=-industrial_demand.loc[
            nodes, ["process emission", "process emission from feedstock"]].
        sum(axis=1).sum() / 8760,
    )

    n.add(
        "Link",
        "process emissions",
        bus0="process emissions",
        bus1="co2 atmosphere",
        carrier="process emissions",
        p_nom_extendable=True,
        efficiency=1.0,
    )

    # assume enough local waste heat for CC
    n.madd(
        "Link",
        spatial.co2.locations,
        suffix=" process emissions CC",
        bus0="process emissions",
        bus1="co2 atmosphere",
        bus2=spatial.co2.nodes,
        carrier="process emissions CC",
        p_nom_extendable=True,
        capital_cost=costs.at["cement capture", "fixed"],
        efficiency=1 - costs.at["cement capture", "capture_rate"],
        efficiency2=costs.at["cement capture", "capture_rate"],
        lifetime=costs.at["cement capture", "lifetime"],
    )


def get(item, investment_year=None):
    """Check whether item depends on investment year"""
    if isinstance(item, dict):
        return item[investment_year]
    else:
        return item


"""
Missing data:
 - transport
 - nodal_transport_data
 - cycling_shift
 - dsm_profile
 - avail_profile
"""


def add_land_transport(n, costs):
    """
    Function to add land transport to network
    """
    # TODO options?

    print("adding land transport")

    fuel_cell_share = get(options["land_transport_fuel_cell_share"],
                          investment_year)
    electric_share = get(options["land_transport_electric_share"],
                         investment_year)
    ice_share = 1 - fuel_cell_share - electric_share

    print("FCEV share", fuel_cell_share)
    print("EV share", electric_share)
    print("ICEV share", ice_share)

    assert ice_share >= 0, "Error, more FCEV and EV share than 1."

    # Nodes are already defined, remove it from here
    # nodes = pop_layout.index

    if electric_share > 0:

        n.add("Carrier", "Li ion")

        n.madd("Bus",
               nodes,
               location=nodes,
               suffix=" EV battery",
               carrier="Li ion")

        p_set = (electric_share *
                 (transport[nodes] + cycling_shift(transport[nodes], 1) +
                  cycling_shift(transport[nodes], 2)) / 3)

        n.madd(
            "Load",
            nodes,
            suffix=" land transport EV",
            bus=nodes + " EV battery",
            carrier="land transport EV",
            p_set=p_set,
        )

        p_nom = (nodal_transport_data["number cars"] *
                 options.get("bev_charge_rate", 0.011) * electric_share)

        n.madd(
            "Link",
            nodes,
            suffix=" BEV charger",
            bus0=nodes,
            bus1=nodes + " EV battery",
            p_nom=p_nom,
            carrier="BEV charger",
            p_max_pu=avail_profile[nodes],
            efficiency=options.get("bev_charge_efficiency", 0.9),
            # These were set non-zero to find LU infeasibility when availability = 0.25
            # p_nom_extendable=True,
            # p_nom_min=p_nom,
            # capital_cost=1e6,  #i.e. so high it only gets built where necessary
        )

    if electric_share > 0 and options["v2g"]:

        n.madd(
            "Link",
            nodes,
            suffix=" V2G",
            bus1=nodes,
            bus0=nodes + " EV battery",
            p_nom=p_nom,
            carrier="V2G",
            p_max_pu=avail_profile[nodes],
            efficiency=options.get("bev_charge_efficiency", 0.9),
        )

    if electric_share > 0 and options["bev_dsm"]:

        e_nom = (nodal_transport_data["number cars"] *
                 options.get("bev_energy", 0.05) *
                 options["bev_availability"] * electric_share)

        n.madd(
            "Store",
            nodes,
            suffix=" battery storage",
            bus=nodes + " EV battery",
            carrier="battery storage",
            e_cyclic=True,
            e_nom=e_nom,
            e_max_pu=1,
            e_min_pu=dsm_profile[nodes],
        )

    if fuel_cell_share > 0:

        n.madd(
            "Load",
            nodes,
            suffix=" land transport fuel cell",
            bus=nodes + " H2",
            carrier="land transport fuel cell",
            p_set=fuel_cell_share / options["transport_fuel_cell_efficiency"] *
            transport[nodes],
        )

    if ice_share > 0:

        if "EU oil" not in n.buses.index:
            n.add("Bus", "EU oil", location="EU", carrier="oil")

        ice_efficiency = options["transport_internal_combustion_efficiency"]

        n.madd(
            "Load",
            nodes,
            suffix=" land transport oil",
            bus="EU oil",
            carrier="land transport oil",
            p_set=ice_share / ice_efficiency * transport[nodes],
        )

        co2 = (ice_share / ice_efficiency * transport[nodes].sum().sum() /
               8760 * costs.at["oil", "CO2 intensity"])

        n.add(
            "Load",
            "land transport oil emissions",
            bus="co2 atmosphere",
            carrier="land transport oil emissions",
            p_set=-co2,
        )


if __name__ == "__main__":
    if "snakemake" not in globals():
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

        # from helper import mock_snakemake #TODO remove func from here to helper script
        snakemake = mock_snakemake("prepare_sector_network",
                                   simpl="",
                                   clusters="4")
    # TODO add mock_snakemake func

    # TODO fetch from config

    n = pypsa.Network(snakemake.input.network)

    nodes = n.buses.index

    # costs = pd.read_csv( "{}/pypsa-earth-sec/data/costs.csv".format(os.path.dirname(os.getcwd())))

    Nyears = n.snapshot_weightings.generators.sum() / 8760

    # TODO fetch investment year from config
    # investment_year = int(snakemake.wildcards.planning_horizons[-4:])

    costs = prepare_costs(
        snakemake.input.costs,
        snakemake.config["costs"]["USD2013_to_EUR2013"],
        snakemake.config["costs"]["discountrate"],
        Nyears,
        snakemake.config["costs"]["lifetime"],
    )
    # TODO logging

    options = snakemake.config["sector"]

    add_co2(n, costs)  # TODO add costs

    # Add_generation() currently adds gas carrier/bus, as defined in config "conventional_generation"
    add_generation(n, costs)

    # Add_oil() adds oil carrier/bus.
    # TODO This might be transferred to add_generation, but before apply remove_elec_base_techs(n) from PyPSA-Eur-Sec
    add_oil(n, costs)

    add_hydrogen(n, costs)  # TODO add costs

    add_storage(n, costs)

    H2_liquid_fossil_conversions(n, costs)

    h2_hc_conversions(n, costs)

    add_industry(n, costs)

    # prepare_transport_data(n)

    # Add_land_transport doesn't run yet, data preparation missing and under progress
    # add_land_transport(n, costs)

    # TODO define spatial (for biomass and co2)

    # TODO changes in case of myopic oversight

    # TODO add co2 tracking function

    # TODO add generation

    # TODO add storage  HERE THE H2 CARRIER IS ADDED IN PYPSA-EUR-SEC

    # TODO add options as in PyPSA-EUR-SEC
