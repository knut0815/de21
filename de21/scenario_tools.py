# -*- coding: utf-8 -*-

"""Work with the scenario data.

Copyright (c) 2016-2018 Uwe Krien <uwe.krien@rl-institut.de>

SPDX-License-Identifier: GPL-3.0-or-later
"""
__copyright__ = "Uwe Krien <uwe.krien@rl-institut.de>"
__license__ = "GPLv3"


# Python libraries
import os

# External libraries
import networkx as nx
from matplotlib import pyplot as plt

# oemof libraries
import oemof.tools.logger as logger
import oemof.solph as solph

# internal modules
import reegis_tools.config as cfg
import reegis_tools.scenario_tools

import de21.basic_scenario


class Scenario(reegis_tools.scenario_tools.Scenario):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def create_nodes(self):
        nodes = nodes_from_table_collection(self.table_collection)
        return nodes


def nodes_from_table_collection(table_collection):
    # Create  a special dictionary that will raise an error if a key is
    # updated. This avoids the
    nodes = reegis_tools.scenario_tools.NodeDict()

    # Global commodity sources
    cs = table_collection['commodity_source']['DE']
    for fuel in cs.columns:
        bus_label = 'bus_cs_{0}'.format(fuel.replace(' ', '_'))
        nodes[bus_label] = solph.Bus(label=bus_label)

        cs_label = 'source_cs_{0}'.format(fuel.replace(' ', '_'))
        nodes[cs_label] = solph.Source(
            label=cs_label, outputs={nodes[bus_label]: solph.Flow(
                variable_costs=cs.loc['costs', fuel],
                emission=cs.loc['emission', fuel])})

    # Local volatile sources
    vs = table_collection['volatile_source']
    ts = table_collection['time_series']
    for region in vs.columns.get_level_values(0).unique():
        for vs_type in vs[region].columns:
            vs_label = 'source_{0}_{1}'.format(vs_type, region)
            capacity = vs.loc['capacity', (region, vs_type)]
            try:
                feedin = ts[region, vs_type]
            except KeyError:
                if capacity > 0:
                    msg = "Missing time series for {0} (capacity: {1}) in {2}."
                    raise ValueError(msg.format(vs_type, capacity, region))
            bus_label = 'bus_elec_{0}'.format(region)
            if bus_label not in nodes:
                nodes[bus_label] = solph.Bus(label=bus_label)
            if capacity * sum(feedin) > 0:
                nodes[vs_label] = solph.Source(
                    label=vs_label,
                    outputs={nodes[bus_label]: solph.Flow(
                        actual_value=feedin, nominal_value=capacity,
                        fixed=True, emission=0)})

    # Decentralised heating systems
    dh = table_collection['decentralised_heating']
    for fuel in dh['DE_demand'].columns:
        src = dh.loc['source', ('DE_demand', fuel)]
        bus_label = 'bus_cs_{0}'.format(src.replace(' ', '_'))

        # Check if source bus exists
        if bus_label not in nodes:
            raise ValueError("Heating without source!")

        # Create heating bus as Bus
        heat_bus_label = 'bus_dectrl_heating_{0}'.format(
            fuel.replace(' ', '_'))
        nodes[heat_bus_label] = solph.Bus(label=heat_bus_label)

        # Create heating system as Transformer
        trsf_label = 'trsf_dectrl_heating_{0}'.format(fuel.replace(' ', '_'))
        efficiency = float(dh.loc['efficiency', ('DE_demand', fuel)])
        nodes[trsf_label] = solph.Transformer(
            label=trsf_label,
            inputs={nodes[bus_label]: solph.Flow()},
            outputs={nodes[heat_bus_label]: solph.Flow()},
            conversion_factors={nodes[heat_bus_label]: efficiency})

        # Create demand as Sink
        d_heat_demand_label = 'demand_dectrl_heating_{0}'.format(
            fuel.replace(' ', '_'))
        nodes[d_heat_demand_label] = solph.Sink(
                label=d_heat_demand_label,
                inputs={nodes[heat_bus_label]: solph.Flow(
                    actual_value=ts['DE_demand', fuel],
                    nominal_value=1, fixed=True)})

    # Local electricity demand
    ts.columns = ts.columns.swaplevel()
    for region in ts['electrical_load'].columns:
        if ts['electrical_load'][region].sum() > 0:
            bus_label = 'bus_elec_{0}'.format(region)
            if bus_label not in nodes:
                nodes[bus_label] = solph.Bus(label=bus_label)
            elec_demand_label = 'demand_elec_{0}'.format(region)
            nodes[elec_demand_label] = solph.Sink(
                label=elec_demand_label,
                inputs={nodes[bus_label]: solph.Flow(
                    actual_value=ts['electrical_load', region],
                    nominal_value=1, fixed=True)})

    # Local district heating demand
    for region in ts['district_heating'].columns:
        if ts['district_heating'][region].sum() > 0:
            bus_label = 'bus_distr_heat_{0}'.format(region)
            if bus_label not in nodes:
                nodes[bus_label] = solph.Bus(label=bus_label)
            elec_demand_label = 'demand_distr_heat_{0}'.format(region)
            nodes[elec_demand_label] = solph.Sink(
                label=elec_demand_label,
                inputs={nodes[bus_label]: solph.Flow(
                    actual_value=ts['district_heating', region],
                    nominal_value=1, fixed=True)})

    # Connect electricity buses with transmission
    power_lines = table_collection['transmission']['electrical']
    for idx, values in power_lines.iterrows():
        b1, b2 = idx.split('-')
        lines = [(b1, b2), (b2, b1)]
        for line in lines:
            line_label = 'power_line_{0}_{1}'.format(line[0], line[1])
            bus_label_in = 'bus_elec_{0}'.format(line[0])
            bus_label_out = 'bus_elec_{0}'.format(line[1])
            if bus_label_in not in nodes:
                raise ValueError(
                    "Bus {0} missing for power line from {0} to {1}".format(
                        bus_label_in, bus_label_out))
            if bus_label_out not in nodes:
                raise ValueError(
                    "Bus {0} missing for power line from {0} to {1}".format(
                        bus_label_out, bus_label_in))
            nodes[line_label] = solph.Transformer(
                label=line_label,
                inputs={nodes[bus_label_in]: solph.Flow()},
                outputs={nodes[bus_label_out]: solph.Flow(
                    nominal_value=values.capacity)},
                conversion_factors={nodes[bus_label_out]: values.efficiency})

    # Local power plants as Transformer and ExtractionTurbineCHP (chp)
    trsf = table_collection['transformer']
    for region in trsf.columns.get_level_values(0).unique():
        bus_heat = 'bus_distr_heat_{0}'.format(region)
        bus_elec = 'bus_elec_{0}'.format(region)
        for fuel in trsf[region].columns:
            bus_fuel = 'bus_cs_{0}'.format(fuel.replace(' ', '_'))
            params = trsf[region, fuel]

            # Create power plants as 1x1 Transformer
            if params.capacity > 0:
                # Define output flow with or without sum_max attribute
                if params.limit_elec_pp == float('inf'):
                    outflow = solph.Flow(nominal_value=params.capacity)
                else:
                    outflow = solph.Flow(nominal_value=params.capacity,
                                         sum_max=params.limit_elec_pp)

                trsf_label = 'trsf_pp_{0}_{1}'.format(
                    region, fuel.replace(' ', '_'))
                nodes[trsf_label] = solph.Transformer(
                    label=trsf_label,
                    inputs={nodes[bus_fuel]: solph.Flow()},
                    outputs={nodes[bus_elec]: outflow},
                    conversion_factors={nodes[bus_elec]: params.efficiency})

            # Create chp plants as 1x2 Transformer
            if params.capacity_heat_chp > 0:
                trsf_label = 'trsf_chp_{0}_{1}'.format(
                    region, fuel.replace(' ', '_'))
                nodes[trsf_label] = solph.Transformer(
                    label=trsf_label,
                    inputs={nodes[bus_fuel]: solph.Flow()},
                    outputs={
                        nodes[bus_elec]: solph.Flow(),
                        nodes[bus_heat]: solph.Flow(
                            nominal_value=params.capacity_heat_chp,
                            sum_max=params.limit_heat_chp)},
                    conversion_factors={
                        nodes[bus_elec]: params.efficiency_elec_chp,
                        nodes[bus_elec]: params.efficiency_heat_chp})

            # Create heat plants as 1x1 Transformer
            if params.capacity_hp > 0:
                trsf_label = 'trsf_hp_{0}_{1}'.format(
                    region, fuel.replace(' ', '_'))
                nodes[trsf_label] = solph.Transformer(
                    label=trsf_label,
                    inputs={nodes[bus_fuel]: solph.Flow()},
                    outputs={nodes[bus_heat]: solph.Flow(
                        nominal_value=params.capacity_hp,
                        sum_max=params.limit_hp)},
                    conversion_factors={nodes[bus_heat]: params.efficiency_hp})

    # Storages
    storages = table_collection['storages']
    storages.columns = storages.columns.swaplevel()
    for region in storages['phes'].columns:
        storage_label = 'phe_storage_{0}'.format(region)
        bus_label = 'bus_elec_{0}'.format(region)
        params = storages['phes'][region]
        nodes[storage_label] = solph.components.GenericStorage(
            label=storage_label,
            inputs={nodes[bus_label]: solph.Flow(
                nominal_value=params.pump)},
            outputs={nodes[bus_label]: solph.Flow(
                nominal_value=params.turbine)},
            nominal_capacity=params.energy,
            capacity_loss=0,
            initial_capacity=None,
            inflow_conversion_factor=params.pump_eff,
            outflow_conversion_factor=params.turbine_eff)

    # Add shortage excess to every bus
    bus_keys = [key for key in nodes.keys() if 'bus' in key]
    for key in bus_keys:
        excess_label = 'excess_{0}'.format(key)
        nodes[excess_label] = solph.Sink(
            label=excess_label,
            inputs={nodes[key]: solph.Flow()})
        shortage_label = 'shortage_{0}'.format(key)
        nodes[shortage_label] = solph.Source(
            label=shortage_label,
            outputs={nodes[key]: solph.Flow(variable_costs=9000)})

    return nodes


def draw_graph(grph, edge_labels=True, node_color='#AFAFAF',
               edge_color='#CFCFCF', plot=True, node_size=2000,
               with_labels=True, arrows=True, layout='neato'):
    """
    Draw a graph. This function will be removed in future versions.

    Parameters
    ----------
    grph : networkxGraph
        A graph to draw.
    edge_labels : boolean
        Use nominal values of flow as edge label
    node_color : dict or string
        Hex color code oder matplotlib color for each node. If string, all
        colors are the same.

    edge_color : string
        Hex color code oder matplotlib color for edge color.

    plot : boolean
        Show matplotlib plot.

    node_size : integer
        Size of nodes.

    with_labels : boolean
        Draw node labels.

    arrows : boolean
        Draw arrows on directed edges. Works only if an optimization_model has
        been passed.
    layout : string
        networkx graph layout, one of: neato, dot, twopi, circo, fdp, sfdp.
    """
    if type(node_color) is dict:
        node_color = [node_color.get(g, '#AFAFAF') for g in grph.nodes()]

    # set drawing options
    options = {
     'prog': 'dot',
     'with_labels': with_labels,
     'node_color': node_color,
     'edge_color': edge_color,
     'node_size': node_size,
     'arrows': arrows
    }

    # draw graph
    pos = nx.drawing.nx_agraph.graphviz_layout(grph, prog=layout)

    nx.draw(grph, pos=pos, **options)

    # add edge labels for all edges
    if edge_labels is True and plt:
        labels = nx.get_edge_attributes(grph, 'weight')
        nx.draw_networkx_edge_labels(grph, pos=pos, edge_labels=labels)

    # show output
    if plot is True:
        plt.show()


def create_basic_scenario(year, round_values=None):
    table_collection = de21.basic_scenario.create_scenario(year, round_values)
    sce = Scenario(table_collection=table_collection, name='basic',
                   year=2014)
    path = os.path.join(cfg.get('paths', 'scenario'), 'basic', str(year))
    sce.to_excel(os.path.join(path, '_'.join([sce.name, str(year)]) + '.xls'))
    sce.to_csv(os.path.join(path, 'csv'))


if __name__ == "__main__":
    logger.define_logging()
    # logger.define_logging(screen_level=logging.WARNING)
    # logging.warning("Only warnings will be displayed!")
    for y in [2014, 2013, 2012]:
        create_basic_scenario(y)
