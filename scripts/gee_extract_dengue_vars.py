#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEE Extractor — Variables climáticas/ambientales por provincia y semana epidemiológica (Ecuador)
Author: Wes Salinas (MIT License for this code)
Data notice: Collections accessed via GEE belong to their providers (NASA, ECMWF, CHIRPS, USGS, etc.).
Do not redistribute original datasets outside their licenses. This script automates downloading from GEE for research.
"""

import ee
import pandas as pd
from datetime import datetime, timedelta
from math import ceil

# ============== CONFIG ==============
GEE_PROJECT = 'earthengine-dengue'        # <--- Cambia al ID de tu proyecto GEE
COUNTRY = 'Ecuador'
YEARS = list(range(2019, 2026))           # 2019–2025
SCALE_DEFAULT = 5000
OUT_CSV = 'gee_variables_ecuador_2019_2025.csv'

# ============== AUTH ==============
def gee_auth_init(project_id: str):
    print('➡ Authenticating and initializing Google Earth Engine...')
    try:
        ee.Initialize(project=project_id)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=project_id)
    print('✔ GEE ready.\n')

# ============== EPI DATES ==============
def epi_sunday(year: int) -> datetime:
    jan1 = datetime(year, 1, 1)
    dow = jan1.weekday()   # Mon=0 ... Sun=6
    first_sat = jan1 + timedelta(days=(5 - dow) % 7)
    if first_sat.day < 4:
        first_sat += timedelta(days=7)
    return first_sat - timedelta(days=6)

def weeks_in_year(year: int) -> int:
    if year == 2020: return 53
    if year == 2025: return 24  # adjust as the year advances
    return 52

def biweekly_windows(year: int, n_weeks: int):
    start = epi_sunday(year)
    blocks = ceil(n_weeks / 2)
    for b in range(blocks):
        d0 = start + timedelta(days=b*14)
        d1 = d0 + timedelta(days=13)
        yield (b+1, d0, d1)

# ============== SOURCES ==============
def get_provinces():
    return (ee.FeatureCollection('FAO/GAUL/2015/level1')
              .filter(ee.Filter.eq('ADM0_NAME', COUNTRY))
              .select(['ADM1_CODE','ADM1_NAME']))

COL_CHIRPS = 'UCSB-CHG/CHIRPS/DAILY'                # precipitation (mm)
COL_ERA5L  = 'ECMWF/ERA5_LAND/HOURLY'               # wind u/v 10m, temperature_2m (K)
COL_MERRA  = 'NASA/GSFC/MERRA/slv/2'                # QV2M (g/kg), T2MDEW (K)
COL_SMAP   = 'NASA/SMAP/SPL4SMGP/007'               # soil moisture
IMG_SRTM   = 'USGS/SRTMGL1_003'                     # elevation
COL_LST    = 'MODIS/061/MOD11A2'                    # LST Day/Night 8-day
COL_VEG    = 'MODIS/061/MOD13Q1'                    # NDVI/EVI 16-day
COL_SR     = 'MODIS/061/MOD09A1'                    # surface reflectance 8-day (for NDWI)
COL_ET     = 'MODIS/061/MOD16A2'                    # Evapotranspiration 8-day

# ============== HELPERS ==============
def ndwi_from_sr(sr_img):
    # NDWI = (Green - NIR) / (Green + NIR) ; MOD09A1: b04=Green, b02=NIR
    green = sr_img.select('sur_refl_b04').multiply(1e-4)
    nir   = sr_img.select('sur_refl_b02').multiply(1e-4)
    return green.subtract(nir).divide(green.add(nir)).rename('ndwi')

# ============== COMPOSITES ==============
def compose_window(d0, d1):
    chirps = (ee.ImageCollection(COL_CHIRPS)
                .filterDate(d0, d1)
                .select('precipitation')
                .sum()
                .rename('precipitacion_mm'))
    # alias for convenience
    precip_mm_alias = chirps.rename('precip_mm')

    era = (ee.ImageCollection(COL_ERA5L)
            .filterDate(d0, d1)
            .select(['u_component_of_wind_10m','v_component_of_wind_10m','temperature_2m']))
    era_mean = era.mean()
    wind_ms = era_mean.expression(
        'sqrt(u*u + v*v)',
        {'u': era_mean.select('u_component_of_wind_10m'),
         'v': era_mean.select('v_component_of_wind_10m')}
    ).rename('indice_viento_m_s')
    temp_mean_c = era_mean.select('temperature_2m').subtract(273.15).rename('temp_mean_c')

    merra_mean = (ee.ImageCollection(COL_MERRA)
                    .filterDate(d0, d1)
                    .select(['QV2M','T2MDEW'])
                    .mean())
    spec_hum_gkg = merra_mean.select('QV2M').rename('spec_hum_gkg')
    dewpt_c = merra_mean.select('T2MDEW').subtract(273.15).rename('dewpt_c')

    # RH from T and Td using Magnus equation
    rh = era_mean.expression(
        '100 * (exp((17.625*TD)/(243.04+TD)) / exp((17.625*T)/(243.04+T)))',
        {'T': era_mean.select('temperature_2m').subtract(273.15),
         'TD': merra_mean.select('T2MDEW').subtract(273.15)}
    ).rename('humedad_relativa_pct')

    # min/max/dtr using ERA5L percentiles
    t_coll = ee.ImageCollection(COL_ERA5L).filterDate(d0, d1).select('temperature_2m')
    t_p = t_coll.reduce(ee.Reducer.percentile([10,90]))
    temp_min_c = t_p.select('temperature_2m_p10').subtract(273.15).rename('temp_min_c')
    temp_max_c = t_p.select('temperature_2m_p90').subtract(273.15).rename('temp_max_c')
    rango_diurno_temp_dtr = temp_max_c.subtract(temp_min_c).rename('rango_diurno_temp_dtr')

    soil_moist_pct = (ee.ImageCollection(COL_SMAP)
                        .filterDate(d0, d1)
                        .select('sm_surface')
                        .mean()
                        .rename('soil_moist_pct'))

    elev_m = ee.Image(IMG_SRTM).select('elevation').rename('elev_m')
    slope_deg = ee.Terrain.slope(ee.Image(IMG_SRTM)).rename('slope_deg')

    lst = (ee.ImageCollection(COL_LST)
             .filterDate(d0, d1)
             .select(['LST_Day_1km','LST_Night_1km'])
             .mean())
    lst_day_c = lst.select('LST_Day_1km').multiply(0.02).subtract(273.15).rename('lst_day_c')
    lst_night_c = lst.select('LST_Night_1km').multiply(0.02).subtract(273.15).rename('lst_night_c')

    veg = (ee.ImageCollection(COL_VEG)
             .filterDate(d0, d1)
             .select(['NDVI','EVI'])
             .mean())
    ndvi = veg.select('NDVI').multiply(0.0001).rename('ndvi')
    evi = veg.select('EVI').multiply(0.0001).rename('evi')

    sr_mean = (ee.ImageCollection(COL_SR)
                 .filterDate(d0, d1)
                 .select(['sur_refl_b04','sur_refl_b02'])
                 .mean())
    ndwi = ndwi_from_sr(sr_mean)

    # Evapotranspiration (mm)
    et_mm = (ee.ImageCollection(COL_ET)
               .filterDate(d0, d1)
               .select('ET')
               .mean()
               .multiply(0.1)  # scale to mm
               .rename('et_mm'))

    return ee.Image.cat([
        chirps, precip_mm_alias, wind_ms, temp_mean_c, temp_min_c, temp_max_c, rango_diurno_temp_dtr,
        spec_hum_gkg, dewpt_c, rh, soil_moist_pct, elev_m, slope_deg,
        lst_day_c, lst_night_c, ndvi, evi, ndwi, et_mm
    ])

def reduce_to_provinces(img, provincias, scale=SCALE_DEFAULT):
    return img.reduceRegions(
        collection=provincias,
        reducer=ee.Reducer.mean(),
        scale=scale,
        crs='EPSG:4326',
        tileScale=4
    )

def get_provinces():
    return (ee.FeatureCollection('FAO/GAUL/2015/level1')
              .filter(ee.Filter.eq('ADM0_NAME', COUNTRY))
              .select(['ADM1_CODE','ADM1_NAME']))

def run():
    gee_auth_init(GEE_PROJECT)
    provincias = get_provinces()
    regs = []

    for year in YEARS:
        n_weeks = weeks_in_year(year)
        print(f'➡ {year}: {n_weeks} weeks')
        for b_idx, d0, d1 in biweekly_windows(year, n_weeks):
            print(f'  ▸ Block {b_idx}: {d0.date()} → {d1.date()}')
            img = compose_window(d0, d1)
            feats = reduce_to_provinces(img, provincias).getInfo()['features']

            wk1 = (b_idx - 1) * 2 + 1
            for offset in (0,1):
                wk = wk1 + offset
                if wk > n_weeks:
                    break
                for f in feats:
                    p = f['properties']
                    regs.append({
                        'anio'                    : year,
                        'semana_epi'              : wk,
                        'codigo_provincia'        : p.get('ADM1_CODE'),
                        'provincia'               : p.get('ADM1_NAME'),
                        'precipitacion_mm'        : p.get('precipitacion_mm'),
                        'precip_mm'               : p.get('precip_mm'),
                        'temp_mean_c'             : p.get('temp_mean_c'),
                        'temp_min_c'              : p.get('temp_min_c'),
                        'temp_max_c'              : p.get('temp_max_c'),
                        'rango_diurno_temp_dtr'   : p.get('rango_diurno_temp_dtr'),
                        'humedad_relativa_pct'    : p.get('humedad_relativa_pct'),
                        'spec_hum_gkg'            : p.get('spec_hum_gkg'),
                        'dewpt_c'                 : p.get('dewpt_c'),
                        'soil_moist_pct'          : p.get('soil_moist_pct'),
                        'elev_m'                  : p.get('elev_m'),
                        'slope_deg'               : p.get('slope_deg'),
                        'indice_viento_m_s'       : p.get('indice_viento_m_s'),
                        'ndvi'                    : p.get('ndvi'),
                        'evi'                     : p.get('evi'),
                        'ndwi'                    : p.get('ndwi'),
                        'lst_day_c'               : p.get('lst_day_c'),
                        'lst_night_c'             : p.get('lst_night_c'),
                        'et_mm'                   : p.get('et_mm'),
                    })

    df = pd.DataFrame(regs)
    df.sort_values(['anio','semana_epi','codigo_provincia'], inplace=True)
    df.to_csv(OUT_CSV, index=False, encoding='utf-8')
    print(f'✅ Exported: {OUT_CSV} ({len(df):,} rows)')

if __name__ == '__main__':
    run()
