"""Test functions in generation.py"""
import logging
import sqlite3
import os
from pathlib import Path
from powergenome.GenX import (
    RESOURCE_TAGS,
    add_cap_res_network,
    add_misc_gen_values,
    check_resource_tags,
    create_policy_req,
    create_regional_cap_res,
    hydro_energy_to_power,
    max_cap_req,
    min_cap_req,
    network_line_loss,
    network_max_reinforcement,
    network_reinforcement_cost,
    reduce_time_domain,
    round_col_values,
    set_int_cols,
)
from powergenome.eia_opendata import add_user_fuel_prices
from powergenome.external_data import make_generator_variability

from powergenome.fuels import fuel_cost_table
from powergenome.nrelatb import db_col_values

CWD = Path.cwd()
# os.environ["RESOURCE_GROUPS"] = str(CWD / "data" / "resource_groups_base")
# os.environ["PUDL_DB"] = "sqlite:////" + str(
#     CWD / "tests" / "data" / "pudl_test_data.db"
# )
# os.environ["PG_DB"] = "sqlite:////" + str(
#     CWD / "tests" / "data" / "pg_misc_tables.sqlite3"
# )

import numpy as np
import pandas as pd
import sqlalchemy
import powergenome
import pytest
from powergenome.generators import (
    fill_missing_tech_descriptions,
    gentype_region_capacity_factor,
    group_technologies,
    label_retirement_year,
    label_small_hydro,
    load_demand_response_efs_profile,
    remove_leading_zero,
    unit_generator_heat_rates,
    load_860m,
    GeneratorClusters,
    energy_storage_mwh,
)
from powergenome.load_profiles import (
    add_load_growth,
    make_final_load_curves,
    make_load_curves,
)
from powergenome.params import DATA_PATHS, SETTINGS  # , SETTINGS
from powergenome.transmission import (
    agg_transmission_constraints,
    transmission_line_distance,
)
from powergenome.util import (
    build_scenario_settings,
    find_region_col,
    init_pudl_connection,
    check_settings,
    load_settings,
    map_agg_region_names,
    regions_to_keep,
    remove_fuel_scenario_name,
    reverse_dict_of_lists,
    write_results_file,
)

logger = logging.getLogger(powergenome.__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter(
    # More extensive test-like formatter...
    "%(asctime)s [%(levelname)8s] %(name)s:%(lineno)s %(message)s",
    # This is the datetime format string.
    "%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# pudl_engine = sqlite3.connect(DATA_PATHS["test_data"] / "pudl_test_data.db")
# pg_engine = sqlalchemy.create_engine(
#     "sqlite:////" + str(DATA_PATHS["test_data"] / "pg_misc_tables.sqlite3")
# )
if os.name == "nt":
    # if user is using a windows system
    sql_prefix = "sqlite:///"
else:
    sql_prefix = "sqlite:////"
pudl_engine, pudl_out, pg_engine = init_pudl_connection(
    start_year=2018,
    end_year=2020,
    pudl_db=sql_prefix + str(DATA_PATHS["test_data"] / "pudl_test_data.db"),
    pg_db=sql_prefix + str(DATA_PATHS["test_data"] / "pg_misc_tables.sqlite3"),
)


@pytest.fixture(scope="module")
def generation_fuel_eia923_data():
    gen_fuel = pd.read_sql_query(
        "SELECT * FROM generation_fuel_eia923",
        pudl_engine,
        parse_dates=["report_date"],
    )
    return gen_fuel


@pytest.fixture(scope="module")
def generators_eia860_data():
    sql = """
        SELECT *
        FROM generators_eia860
        WHERE operational_status_code = 'OP'
    """
    gens_860 = pd.read_sql_query(
        sql, pudl_engine, parse_dates=["report_date", "planned_retirement_date"]
    )
    return gens_860


@pytest.fixture(scope="module")
def generators_entity_eia_data():
    gen_entity = pd.read_sql_query(
        "SELECT * FROM generators_entity_eia",
        pudl_engine,
        parse_dates=["operating_date"],
    )
    return gen_entity


@pytest.fixture(scope="module")
def plant_region_map_ipm_data():
    plant_region_map = pd.read_sql_query(
        "SELECT * FROM plant_region_map_epaipm", pudl_engine
    )
    return plant_region_map


@pytest.fixture(scope="module")
def test_settings():
    settings = load_settings(DATA_PATHS["test_data"] / "test_settings.yml")
    settings["RESOURCE_GROUPS"] = DATA_PATHS["test_data"] / "resource_groups_base"
    return settings


@pytest.fixture(scope="module")
def CA_AZ_settings():
    settings = load_settings(
        DATA_PATHS["powergenome"].parent / "example_systems" / "CA_AZ" / "settings"
    )
    settings["input_folder"] = Path(
        DATA_PATHS["powergenome"].parent
        / "example_systems"
        / "CA_AZ"
        / settings["input_folder"]
    )
    settings["RESOURCE_GROUPS"] = DATA_PATHS["test_data"] / "resource_groups_base"
    scenario_definitions = pd.read_csv(
        settings["input_folder"] / settings["scenario_definitions_fn"]
    )
    scenario_settings = build_scenario_settings(settings, scenario_definitions)

    return scenario_settings[2030]["p1"]


class MockPudlOut:
    """
    The methods in this class read pre-calculated tables from a sqlite db and return
    the expected values from pudl_out methods.
    """

    def hr_by_unit():
        "Heat rate by unit over multiple years"
        hr_by_unit = pd.read_sql_query(
            "SELECT * FROM hr_by_unit", pudl_engine, parse_dates=["report_date"]
        )
        return hr_by_unit

    def bga():
        "Boiler generator associations with unit_id_pudl values"
        bga = pd.read_sql_query(
            "SELECT * FROM boiler_generator_assn_eia860", pudl_engine
        )
        return bga


def test_load_single_settings():
    settings = load_settings(
        DATA_PATHS["powergenome"].parent
        / "example_systems"
        / "CA_AZ"
        / "combined_settings.yml"
    )


def test_load_multiple_settings():
    settings = load_settings(
        DATA_PATHS["powergenome"].parent / "example_systems" / "CA_AZ" / "settings"
    )


def test_group_technologies(generators_eia860_data, test_settings):
    df = generators_eia860_data.loc[
        generators_eia860_data.report_date.dt.year == 2020, :
    ]
    # df = df.query("report_date.dt.year==2017")
    df = df.drop_duplicates(subset=["plant_id_eia", "generator_id"])

    grouped_by_tech = group_technologies(
        df,
        test_settings.get("group_technologies"),
        test_settings.get("tech_groups", {}) or {},
        test_settings.get("regional_no_grouping", {}) or {},
    )
    techs = grouped_by_tech["technology_description"].unique()
    capacities = grouped_by_tech.groupby("technology_description")[
        test_settings["capacity_col"]
    ].sum()
    # expected_hydro_cap = 48.1
    hydro_cap = capacities["Conventional Hydroelectric"]
    expected_peaker_cap = 354.8
    # peaker_cap = capacities["Peaker"]

    assert len(df) == len(grouped_by_tech)
    assert df["capacity_mw"].sum() == grouped_by_tech["capacity_mw"].sum()
    assert "Peaker" in techs
    # assert np.allclose(hydro_cap, expected_hydro_cap)
    # assert np.allclose(peaker_cap, expected_peaker_cap)


def test_fill_missing_tech_descriptions(generators_eia860_data):
    filled = fill_missing_tech_descriptions(generators_eia860_data)

    assert len(generators_eia860_data) == len(
        filled.dropna(subset=["technology_description"])
    )


def test_label_small_hyro(
    generators_eia860_data, test_settings, plant_region_map_ipm_data
):
    region_agg_map = reverse_dict_of_lists(test_settings.get("region_aggregations", {}))
    model_region_map_df = map_agg_region_names(
        df=plant_region_map_ipm_data,
        region_agg_map=region_agg_map,
        original_col_name="region",
        new_col_name="model_region",
    )
    df = pd.merge(
        generators_eia860_data, model_region_map_df, on="plant_id_eia", how="left"
    )
    logger.info(df[["plant_id_eia", "technology_description", "model_region"]].head())

    # df["model_region"] = df["region"].map(reverse_dict_of_lists)

    df = label_small_hydro(df, test_settings, by=["plant_id_eia", "report_date"])
    print(df.query("plant_id_eia==34"))
    logger.info(df["technology_description"].unique())

    assert "Small Hydroelectric" in df["technology_description"].unique()
    assert np.allclose(
        df.loc[df.technology_description == "Small Hydroelectric", "capacity_mw"].sum(),
        140.5,
    )


# def test_label_retirement_year(
#     generators_eia860_data, generators_entity_eia_data, test_settings
# ):
#     gens = pd.merge(
#         generators_eia860_data,
#         generators_entity_eia_data,
#         on=["plant_id_eia", "generator_id"],
#         how="left",
#     )
#     df = label_retirement_year(gens, test_settings)
#     print(df)

#     assert df.loc[df["retirement_year"].isnull(), :].empty is True


def test_unit_generator_heat_rates(data_years=[2020]):
    hr_df = unit_generator_heat_rates(MockPudlOut, data_years)

    assert hr_df.empty is False
    assert "heat_rate_mmbtu_mwh" in hr_df.columns
    assert np.allclose(
        hr_df.query("plant_id_eia==117 & unit_id_pudl == 2")[
            "heat_rate_mmbtu_mwh"
        ].values,
        [7.635626],
    )


def test_load_860m(test_settings):
    eia_860m = load_860m(test_settings)
    test_settings["eia_860m_fn"] = None
    eia_860m = load_860m(test_settings)


def test_agg_transmission_constraints(test_settings):
    agg_transmission_constraints(pg_engine, test_settings)


def test_demand_curve(test_settings):
    make_load_curves(pg_engine, test_settings)


def test_alt_table_load_sources(CA_AZ_settings):
    # Test with a single non-default load table
    CA_AZ_settings["load_source_table_name"] = {
        # "EFS": "load_curves_nrel_efs",
        "FERC": "load_curves_ferc",
    }
    CA_AZ_settings["regional_load_source"] = "FERC"
    CA_AZ_settings["EFS_DATA"] = DATA_PATHS["test_data"] / "efs"
    make_final_load_curves(pg_engine, CA_AZ_settings)


def test_combined_load_sources(CA_AZ_settings):
    # Test with a combination of user and database load sources
    CA_AZ_settings["regional_load_fn"] = "test_regional_load_profiles.csv"
    CA_AZ_settings["load_source_table_name"] = {"EFS": "load_curves_nrel_efs"}
    CA_AZ_settings["regional_load_source"] = {
        "USER": ["CA_N", "CA_S"],
        "EFS": ["WECC_AZ"],
    }
    CA_AZ_settings["load_source_table_name"] = {
        "EFS": "load_curves_nrel_efs",
        "FERC": "load_curves_ferc",
    }
    CA_AZ_settings["EFS_DATA"] = DATA_PATHS["test_data"] / "efs"
    make_final_load_curves(pg_engine, CA_AZ_settings)


def test_check_settings(test_settings):
    check_settings(test_settings, pg_engine)


# def test_gentype_region_capacity_factor(plant_region_map_ipm_data, test_settings):
#     cf_techs = test_settings["capacity_factor_techs"]

#     plant_region_map_ipm_data = plant_region_map_ipm_data.rename(
#         columns={"region": "model_region"}
#     )
#     df = gentype_region_capacity_factor(
#         pudl_engine, plant_region_map_ipm_data, test_settings
#     )
#     print(df.technology.unique())
#     assert "Biomass" in df.technology.unique()
#     # CF can sometime be greater than 1, but shouldn't be significantly higher.
#     assert df.loc[df["technology"].isin(cf_techs), "capacity_factor"].max() < 2


def test_gen_integration(CA_AZ_settings, tmp_path):
    CA_AZ_settings["atb_modifiers"] = {
        "ngccccs": {
            "technology": "NaturalGas",
            "tech_detail": "CCCCSAvgCF",
            "Heat_Rate_MMBTU_per_MWh": 7.159,
        }
    }
    CA_AZ_settings["modified_atb_new_gen"]["NGCCS100"]["heat_rate"] = 7.5
    CA_AZ_settings["EFS_DATA"] = DATA_PATHS["test_data"] / "efs"
    gc = GeneratorClusters(
        pudl_engine, pudl_out, pg_engine, CA_AZ_settings, supplement_with_860m=True
    )
    all_gens = gc.create_all_generators()
    all_gens = add_misc_gen_values(all_gens, CA_AZ_settings)

    assert (
        all_gens.loc[all_gens["Resource"] == "CA_N_biomass_1", "Eff_Up"].values[0] == 1
    )
    assert np.allclose(
        all_gens.query("technology.str.contains('NaturalGas_CCCCS', case=False)")[
            "Heat_Rate_MMBTU_per_MWh"
        ].mean(),
        7.159,
    )
    assert np.allclose(
        all_gens.query("technology.str.contains('CCS100', case=False)")[
            "Heat_Rate_MMBTU_per_MWh"
        ].mean(),
        7.5,
    )
    # Capacity in existing clusters should be > 0
    assert all(gc.results["Existing_Cap_MW"] > 0)
    batteries = gc.results.query("technology == 'Batteries'")

    # Battery energy capacity should be larger than battery power capacity
    assert all(batteries["Existing_Cap_MWh"] > batteries["Existing_Cap_MW"])

    gen_variability = make_generator_variability(all_gens)
    assert (gen_variability >= 0).all().all()

    fuels = fuel_cost_table(
        fuel_costs=gc.fuel_prices,
        generators=gc.all_resources,
        settings=gc.settings,
    )
    fuels.index.name = "Time_Index"
    write_results_file(
        df=remove_fuel_scenario_name(fuels, gc.settings)
        .pipe(set_int_cols)
        .pipe(round_col_values),
        folder=tmp_path,
        file_name="Fuels_data.csv",
        include_index=True,
    )
    load = make_final_load_curves(pg_engine=pg_engine, settings=gc.settings)
    (
        reduced_resource_profile,
        reduced_load_profile,
        time_series_mapping,
        representative_point,
    ) = reduce_time_domain(gen_variability, load, gc.settings)
    if gc.settings["reduce_time_domain"]:
        assert len(representative_point) == gc.settings["time_domain_periods"]
        assert (
            time_series_mapping["Rep_Period"].nunique()
            == gc.settings["time_domain_periods"]
        )
        assert representative_point.isna().any().all() == False
        assert time_series_mapping.isna().any().all() == False
    assert len(reduced_load_profile) == len(reduced_resource_profile)

    gc.settings["distributed_gen_method"]["CA_N"] = "fraction_load"
    gc.settings["distributed_gen_values"][2030]["CA_N"] = 0.1
    gc.settings["regional_load_fn"] = "test_regional_load_profiles.csv"
    gc.settings["regional_load_includes_demand_response"] = False
    make_final_load_curves(pg_engine=pg_engine, settings=gc.settings)

    model_regions_gdf = gc.model_regions_gdf
    transmission = (
        agg_transmission_constraints(pg_engine=pg_engine, settings=gc.settings)
        .pipe(
            transmission_line_distance,
            ipm_shapefile=model_regions_gdf,
            settings=gc.settings,
            units="mile",
        )
        .pipe(network_line_loss, settings=gc.settings)
        .pipe(network_max_reinforcement, settings=gc.settings)
        .pipe(network_reinforcement_cost, settings=gc.settings)
        .pipe(set_int_cols)
        .pipe(round_col_values)
        .pipe(add_cap_res_network, settings=gc.settings)
    )

    if gc.settings.get("emission_policies_fn"):
        energy_share_req = create_policy_req(gc.settings, col_str_match="ESR")
        co2_cap = create_policy_req(gc.settings, col_str_match="CO_2")
    min_cap = min_cap_req(gc.settings)

    cap_res = create_regional_cap_res(gc.settings)


def test_existing_gen_profiles():
    ipm_regions = pd.read_sql_table("regions_entity_epaipm", pg_engine)
    regions = [r for r in ipm_regions.region_id_epaipm.to_list() if "CN_" not in r]

    s = """
    SELECT DISTINCT technology_description
    FROM generators_eia860
    """
    technologies = (
        pd.read_sql_query(s, pudl_engine).dropna()["technology_description"].to_list()
    )
    technologies.remove("Natural Gas with Compressed Air Storage")
    settings = dict(
        RESOURCE_GROUPS=DATA_PATHS["test_data"] / "resource_groups_base",
        target_usd_year=2019,
        model_year=2030,
        model_first_planning_year=2022,
        model_regions=regions,
        data_years=[2020],
        capacity_col="capacity_mw",
        num_clusters={tech: 2 for tech in technologies},
        retirement_ages={tech: 200 for tech in technologies},
        atb_data_year=2022,
        atb_existing_year=2020,
        fuel_eia_aeo_year=2022,
        load_eia_aeo_year=2020,
        aeo_fuel_usd_year=2021,
        eia_series_region_names={
            "mountain": "MTN",
        },
        eia_series_fuel_names={
            "naturalgas": "NG",
        },
        eia_series_scenario_names={
            "reference": "REF2022",
        },
        aeo_fuel_scenarios={
            "coal": "reference",
            "naturalgas": "reference",
            "distillate": "reference",
            "uranium": "reference",
        },
        eia_atb_tech_map={
            "Battery": "Battery_*",
            "Batteries": "Battery_*",
            "Biomass": "Biopower_Dedicated",
            "Solar Thermal without Energy Storage": "CSP_Class1",
            "Conventional Steam Coal": "Coal_newAvgCF",
            "Coal Integrated Gasification Combined Cycle": "NaturalGas_CCAvgCF",
            "Natural Gas Fired Combined Cycle": "NaturalGas_CCAvgCF",  # [NaturalGas_CCAvgCF, NETL_NGCC]
            "Natural Gas Fired Combustion Turbine": "NaturalGas_CTAvgCF",
            "Peaker": "NaturalGas_CTAvgCF",
            "Natural Gas Internal Combustion Engine": "NaturalGas_CTAvgCF",
            "Landfill Gas": "NaturalGas_CTAvgCF",
            "Petroleum Liquids": "NaturalGas_CTAvgCF",
            "Municipal Solid Waste": "Biopower_Dedicated",
            "Other Waste Biomass": "Biopower_Dedicated",
            "Wood/Wood Waste Biomass": "Biopower_Dedicated",
            "Solar Photovoltaic": "UtilityPV_Class1",
            "Geothermal": "Geothermal_HydroFlash",  # assume installed capacity is dominated by flash
            "Conventional Hydroelectric": "Hydropower_NSD4",  # Large variability based on choice
            "Hydroelectric Pumped Storage": "Hydropower_NSD4",  # Large variability based on choice
            "Small Hydroelectric": "Hydropower_NSD3",  # Large variability based on choice
            "Onshore Wind Turbine": "LandbasedWind_Class4",  # All onshore wind is the same
            "Offshore Wind Turbine": "OffShoreWind_Class10",  # Mid-range of floating offshore wind
            "Nuclear": "Nuclear_Nuclear",
            "Natural Gas Steam Turbine": "Coal_newAvgCF",  # No gas steam turbines in ATB, using coal instead
            "Solar Thermal with Energy Storage": "CSP_Class1",
            "Solar Thermal without Energy Storage": "CSP_Class1",
            "Other Gases": "NaturalGas_CTAvgCF",
            "Other Natural Gas": "NaturalGas_CTAvgCF",
            "Petroleum Coke": "Coal_newAvgCF",
            "All Other": "NaturalGas_CTAvgCF",
            "Flywheels": "Battery_*",
            "Natural Gas with Compressed Air Storage": "NaturalGas_CTAvgCF",
        },
        startup_vom_costs_mw={
            "coal_small_sub": 2.81,
            "coal_large_sub": 2.69,
            "coal_supercritical": 2.98,
            "gas_cc": 1.03,
            "gas_large_ct": 0.77,
            "gas_aero_ct": 0.70,
            "gas_steam": 1.03,
            "nuclear": 5.4,
        },
        startup_vom_costs_usd_year=2011,
        startup_costs_type="startup_costs_per_cold_start_mw",
        startup_costs_per_cold_start_usd_year=2011,
        startup_costs_per_cold_start_mw={
            "coal_small_sub": 147,
            "coal_large_sub": 105,
            "coal_supercritical": 104,
            "gas_cc": 79,
            "gas_large_ct": 103,
            "gas_aero_ct": 32,
            "gas_steam": 75,
            "nuclear": 210,
        },
        existing_startup_costs_tech_map={
            "Conventional Steam Coal": "coal_large_sub",
            "Natural Gas Fired Combined Cycle": "gas_cc",
            "Natural Gas Fired Combustion Turbine": "gas_large_ct",
            "Natural Gas Steam Turbine": "gas_steam",
            "Nuclear": "nuclear",
        },
    )
    gc = GeneratorClusters(
        pudl_engine, pudl_out, pg_engine, settings, supplement_with_860m=False
    )
    existing_gen = gc.create_region_technology_clusters()
    gen_variability = make_generator_variability(existing_gen)
    assert (gen_variability >= 0).all().all()


def test_cap_req():
    settings = {
        "model_tag_names": ["MinCapTag_1", "MinCapTag_2", "MaxCapTag_1", "MaxCapTag_2"],
        "MinCapReq": {
            "MinCapTag_1": {"description": "Landbasedwind", "min_mw": 8000},
            "MinCapTag_2": {"description": "CA_S_solar", "min_mw": 10000},
        },
        "MaxCapReq": {
            "MaxCapTag_1": {"description": "Landbasedwind", "max_mw": 8000},
            "MaxCapTag_2": {"description": "CA_S_solar", "max_mw": 10000},
        },
        "generator_columns": [],
    }

    max_cap = max_cap_req(settings)
    min_cap = min_cap_req(settings)

    assert set(settings["generator_columns"]) == set(settings["model_tag_names"])
    assert min_cap.isna().any().all() == False
    assert max_cap.isna().any().all() == False


def test_check_resource_tags():
    # Check something that should fail
    cols = ["region", "technology"] + RESOURCE_TAGS
    data = [pd.Series(["a", "b"] + [1] * len(RESOURCE_TAGS), index=cols)]
    df = pd.DataFrame(data)

    with pytest.raises(Exception):
        check_resource_tags(df)

    # Check something that should pass
    cols = ["region", "technology"] + RESOURCE_TAGS
    data = [pd.Series(["a", "b", 1] + [0] * (len(RESOURCE_TAGS) - 1), index=cols)]
    df = pd.DataFrame(data)

    check_resource_tags(df)

    # Check something that should pass
    cols = ["region", "technology"] + RESOURCE_TAGS
    data = [pd.Series(["a", "b", 2] + [0] * (len(RESOURCE_TAGS) - 1), index=cols)]
    df = pd.DataFrame(data)

    check_resource_tags(df)


def test_storage_duration(caplog):
    settings = {
        "energy_storage_duration": {
            "hydroelectric pumped": 15.5,
            "batteries": {"A": 2, "B": 1, "D": 1},
        }
    }

    data = {
        "region": ["A", "A", "A", "B", "B", "B", "C"],
        "technology": ["Hydroelectric Pumped Storage", "Batteries", "Nuclear"] * 2
        + ["Batteries"],
        "Existing_Cap_MW": [1] * 7,
    }
    df = pd.DataFrame(data)

    caplog.set_level(logging.WARNING)
    df_mwh = energy_storage_mwh(
        df,
        settings["energy_storage_duration"],
        "technology",
        "Existing_Cap_MW",
        "Existing_Cap_MWh",
    )
    assert "The regions ['C'] are missing from technology batteries" in caplog.text
    assert "technology 'batteries' has the region 'D'" in caplog.text

    assert df.equals(df_mwh[df.columns])
    mwh = pd.Series([15.5, 2, 0, 15.5, 1, 0, 0])
    assert mwh.equals(df_mwh["Existing_Cap_MWh"])


def test_hydro_energy_to_power():
    settings = {
        "hydro_factor": 2,
        "regional_hydro_factor": {
            "A": 4,
            "B": 1,
        },
    }

    data = {
        "region": ["A", "A", "A", "B", "B", "B", "C"],
        "technology": ["Hydro", "NG", "Nuclear"] * 2 + ["Hydro"],
        "HYDRO": [1, 0, 0] * 2 + [1],
        "profile": [[0.5], np.nan, np.nan] * 2 + [[0.6]],
    }
    df = pd.DataFrame(data)

    df_hydro_ratio = hydro_energy_to_power(
        df, settings["hydro_factor"], settings["regional_hydro_factor"]
    )
    assert df.equals(df_hydro_ratio[df.columns])
    hydro_ratio = pd.Series([2, 0, 0, 1, 0, 0, 1.2])
    assert hydro_ratio.equals(df_hydro_ratio["Hydro_Energy_to_Power_Ratio"])


def test_efs_flex_demand():
    flex_demand = load_demand_response_efs_profile(
        resource="trans_light_duty",
        electrification_stock_fn="EFS_STOCK_AGG.parquet",
        model_year=2034,
        electrification_scenario="REFERENCE ELECTRIFICATION - MODERATE TECHNOLOGY ADVANCEMENT",
        model_regions=["WECC_AZ", "WEC_BANC", "WEC_CALN"],
        region_aggregations={"CA_N": ["WEC_BANC", "WEC_CALN"]},
        path_in=DATA_PATHS["test_data"] / "efs",
    )

    assert set(flex_demand.columns) == set(["WECC_AZ", "CA_N"])
    assert flex_demand.isnull().any().any() == False
    assert flex_demand.min().min() >= 0

    with pytest.raises(ValueError):
        load_demand_response_efs_profile(
            resource="trans_light_duty",
            electrification_stock_fn="EFS_STOCK_AGG.parquet",
            model_year=2034,
            electrification_scenario="REFERENCE ELECTRIFICATION - MODERATE TECHNOLOGY",
            model_regions=["WECC_AZ", "WEC_BANC", "WEC_CALN"],
            region_aggregations={"CA_N": ["WEC_BANC", "WEC_CALN"]},
            path_in=DATA_PATHS["test_data"] / "efs",
        )


def test_flex_resources(CA_AZ_settings):
    CA_AZ_settings["model_year"] = 2035
    CA_AZ_settings["model_first_planning_year"] = 2030
    CA_AZ_settings["electrification_stock_fn"] = "EFS_STOCK_AGG.parquet"
    CA_AZ_settings[
        "electrification_scenario"
    ] = "REFERENCE ELECTRIFICATION - MODERATE TECHNOLOGY ADVANCEMENT"
    CA_AZ_settings["flexible_demand_resources"] = {
        2035: {
            "trans_light_duty": {
                "fraction_shiftable": 0.8,
                "parameter_values": {"Max_DSM_delay": 5, "DR": 2},
            },
            "res_water_heat": {
                "fraction_shiftable": 0.1,
                "parameter_values": {"Max_DSM_delay": 2, "DR": 2},
            },
        }
    }
    CA_AZ_settings["atb_new_gen"] = []
    CA_AZ_settings["renewables_clusters"] = None
    CA_AZ_settings["additional_technologies_fn"] = None
    CA_AZ_settings["modified_atb_new_gen"] = None
    CA_AZ_settings["atb_modifiers"] = None
    CA_AZ_settings["efs_path"] = DATA_PATHS["test_data"] / "efs"

    gc = GeneratorClusters(
        pudl_engine, pudl_out, pg_engine, CA_AZ_settings, current_gens=False
    )
    flex_resources = gc.create_new_generators()

    assert len(flex_resources) == 6
    non_null_cols = ["profile", "Max_DSM_delay", "DR"]
    assert flex_resources[non_null_cols].isnull().any().any() == False


def test_usr_tx(tmp_path):
    settings = {
        "input_folder": tmp_path,
        "user_transmission_constraints_fn": "usr_tx.csv",
        "model_regions": ["A", "B", "C"],
        "tx_value_col": "nonfirm_ttc_mw",
    }

    usr_tx = pd.DataFrame(
        data={
            "region_from": ["A", "C", "C"],
            "region_to": ["B", "A", "B"],
            "nonfirm_ttc_mw": [100, 200, 300],
        }
    )
    usr_tx.to_csv(tmp_path / "usr_tx.csv", index=False)

    tx_constraints = agg_transmission_constraints(pg_engine, settings=settings)

    assert tx_constraints["Line_Max_Flow_MW"].to_list() == [100, 200, 300]

    usr_tx = pd.DataFrame(
        data={
            "region_from": ["A", "C", "C"],
            "region_to": ["B", "A", "B"],
            "firm_ttc_mw": [100, 200, 300],
        }
    )
    usr_tx.to_csv(tmp_path / "usr_tx.csv", index=False)

    settings["tx_value_col"] = "firm_ttc_mw"
    tx_constraints = agg_transmission_constraints(pg_engine, settings=settings)

    assert tx_constraints["Line_Max_Flow_MW"].to_list() == [100, 200, 300]

    usr_tx = pd.DataFrame(
        data={
            "region_from": ["A", "C", "C"],
            "region_to": ["B", "A", "B"],
            "nonfirm_ttc_mw": [100, 200, 300],
        }
    )
    usr_tx = pd.concat(
        [
            usr_tx,
            usr_tx.rename(
                columns={"region_from": "region_to", "region_to": "region_from"}
            ),
        ]
    )
    usr_tx.to_csv(tmp_path / "usr_tx.csv", index=False)

    settings["tx_value_col"] = "nonfirm_ttc_mw"
    tx_constraints = agg_transmission_constraints(pg_engine, settings=settings)

    assert tx_constraints["Line_Max_Flow_MW"].to_list() == [100, 200, 300]

    usr_tx = pd.DataFrame(
        data={
            "region_from": ["A", "C", "C", "A"],
            "region_to": ["B", "A", "B", "B"],
            "nonfirm_ttc_mw": [100, 200, 300, 50],
        }
    )
    usr_tx.to_csv(tmp_path / "usr_tx.csv", index=False)

    with pytest.raises(KeyError):
        agg_transmission_constraints(pg_engine, settings=settings)


def test_load_growth(CA_AZ_settings):
    data = {
        "year": [2019] * 3,
        "region": ["WECC_AZ"] * 3,
        "time_index": [1] * 3,
        "sector": ["commercial", "residential", "industrial"],
        "load_mw": [1, 1, 1],
    }

    load_curves = pd.DataFrame(data)

    load_curves = add_load_growth(load_curves, CA_AZ_settings)

    assert all(load_curves["load_mw"] > 1)

    data = {
        "year": [2017] * 3,
        "region": ["WECC_AZ"] * 3,
        "time_index": [1] * 3,
        "sector": ["commercial", "residential", "industrial"],
        "load_mw": [1, 1, 1],
    }

    load_curves_2 = pd.DataFrame(data)

    load_curves_2 = add_load_growth(load_curves_2, CA_AZ_settings)

    assert all(load_curves["load_mw"] < load_curves_2["load_mw"])

    data = {
        "year": [2019] * 3,
        "region": ["WECC_AZ"] * 3,
        "time_index": [1] * 3,
        "load_mw": [1, 1, 1],
    }

    load_curves_3 = pd.DataFrame(data)

    load_curves_3 = add_load_growth(load_curves_3, CA_AZ_settings)

    assert all(load_curves_3["load_mw"] > 1)
    assert load_curves_3.iloc[0, -1] == load_curves_3.iloc[1, -1]

    data = {
        "year": [2017] * 3,
        "region": ["WECC_AZ"] * 3,
        "time_index": [1] * 3,
        "load_mw": [1, 1, 1],
    }

    load_curves_4 = pd.DataFrame(data)

    load_curves_4 = add_load_growth(load_curves_4, CA_AZ_settings)

    assert load_curves_4.iloc[0, -1] == load_curves_4.iloc[1, -1]
    assert all(load_curves_3["load_mw"] < load_curves_4["load_mw"])


def test_db_col_values():
    values = db_col_values(pg_engine, "technology_costs_nrelatb", ["technology"])
    assert "NaturalGas" in values


def test_find_region_col():
    df = pd.DataFrame(columns=["A", "Region", "C"])
    region_col = find_region_col(df.columns)
    assert region_col == "Region"

    df = pd.DataFrame(columns=["A", "model_region", "C"])
    region_col = find_region_col(df.columns)
    assert region_col == "model_region"

    with pytest.raises(ValueError):
        df = pd.DataFrame(columns=["A", "model_region", "region"])
        region_col = find_region_col(df.columns)

    with pytest.raises(ValueError):
        df = pd.DataFrame(columns=["A", "B", "C"])
        region_col = find_region_col(df.columns)


def test_remove_leading_zeros():
    s = "01"
    assert remove_leading_zero(s) == "1"

    i = 1
    assert remove_leading_zero(i) == 1

    s = "GEN1"
    assert remove_leading_zero(s) == "GEN1"
