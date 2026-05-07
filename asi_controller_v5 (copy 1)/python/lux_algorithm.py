"""
Antarctica Daylight & Filtered Lux Calculator

Hardware:  MidOpt BP470 Blue Bandpass Filter (425–495 nm, peak T >= 90%)
           AMS TSL2591 light-to-digital sensor (I2C, MAX gain = 9876x)

What this script models
1. Solar geometry - declination, day length, polar day/night detection
2. Atmospheric attenuation - Beer-Lambert with Kasten & Young air mass
3. Antarctica snow/ice albedo boost (~85 % albedo)
4. BP470 spectral filtering - only 425-495 nm passes
5. TSL2591 spectral mismatch - sensor peaks at 550-700 nm, not blue
6. Rayleigh scattering enhancement of blue at high air mass
7. TSL2591 gain recommendation and saturation flagging for all four gain modes (LOW 1x, MED 25x, HIGH 428x, MAX 9876x)
8. Raw CH0 count -> calibrated irradiance -> lux conversion

All lux/irradiance values are PEAK SOLAR-NOON estimates, not daily averages.

Dependencies:  numpy  (pip install numpy)

NOTE: This script is a not intended to be used on the actual asi, it is a standalone tool for calculating lux values for a given date and location.
"""

import numpy as np
from datetime import datetime, date, timedelta
import csv
import os


# PHYSICAL & INSTRUMENT CONSTANTS

# -- Solar ---------------------------------------------------------------------
SOLAR_CONSTANT = 1361.0  # W m^-2  (total solar irradiance at 1 AU)
OPTICAL_DEPTH = 0.28  # broadband Rayleigh + aerosol tau, clean polar air
ALBEDO = 0.85  # Antarctica snow/ice surface albedo
ALBEDO_BOOST_FACTOR = 0.45  # fraction of reflected irradiance reaching sensor
LUMINOUS_EFFICACY = 93.0  # lm W^-1 for broadband sunlight (used pre-filter)

# -- BP470 filter (MidOpt) -----------------------------------------------------
BP470_PASSBAND_LOW_NM = 425.0  # nm
BP470_PASSBAND_HIGH_NM = 495.0  # nm
BP470_CENTER_NM = 470.0  # nm
BP470_BANDWIDTH_NM = BP470_PASSBAND_HIGH_NM - BP470_PASSBAND_LOW_NM  # 70 nm
BP470_PEAK_TRANSMISSION = 0.90    # >=90 % per MidOpt spec
# Gaussian passband: integrated effective transmission < peak value
BP470_INTEGRATED_TRANS = 0.85  # conservative integrated value over 70 nm band

# Fraction of total solar irradiance falling in the 425-495 nm window.
# Solar spectral irradiance (AM1.5G) integrates to ~14-17 % in this band.
# Baseline at AM1 (zenith sun); adjusted upward at higher air mass (Rayleigh).
SOLAR_BLUE_FRACTION_BASE = 0.155   # fraction at AM 1

# Rayleigh scattering preferentially removes longer wavelengths at high AM,
# so the blue fraction of *transmitted* irradiance increases with air mass.
# Empirical fit: blue_frac ~ base + slope * (AM - 1), capped at 0.22
RAYLEIGH_BLUE_SLOPE = 0.008   # fraction per unit AM above 1

# -- TSL2591 sensor ------------------------------------------------------------
# CH0 (full-spectrum diode) normalised responsivity at 470 nm relative to its
# peak (~600 nm). Derived from the AMS TSL2591 datasheet spectral curve.
TSL2591_RESPONSIVITY_AT_470NM = 0.45   # ~45 % of peak response

# CH0 irradiance responsivity at 1x gain for white light calibration source:
# ~264 counts per uW cm^-2 at peak wavelength (~600 nm) per datasheet.
# At 470 nm, scale by the relative responsivity above.
TSL2591_COUNTS_PER_UW_CM2_1X_600NM = 264.0
TSL2591_COUNTS_PER_UW_CM2_1X_470NM = (
    TSL2591_COUNTS_PER_UW_CM2_1X_600NM * TSL2591_RESPONSIVITY_AT_470NM
)   # ~119 counts uW^-1 cm^2 at 1x gain, 470 nm

# Photopic luminous efficacy at 470 nm.
# The human eye is only ~6 % as sensitive at 470 nm as at its 555 nm peak.
# Peak photopic efficacy = 683 lm W^-1; at 470 nm the V(lambda) value is ~0.09,
# giving 683 * 0.09 = ~61 lm W^-1. We use 40 lm W^-1 as a conservative
# estimate accounting for the broad passband centred slightly below 470 nm.
PHOTOPIC_EFFICACY_470NM = 40.0   # lm W^-1

# TSL2591 gain modes: {label: (multiplier, ADC_full_scale_counts)}
# The ADC is 16-bit; full scale = 65535 counts for all gain modes.
TSL2591_GAINS = {
    "LOW":  (1,    65535),
    "MED":  (25,   65535),
    "HIGH": (428,  65535),
    "MAX":  (9876, 65535),
}

# Default integration time (seconds). 600 ms = maximum sensitivity setting.
DEFAULT_INTEGRATION_TIME_S = 0.600

# Saturation warning threshold as fraction of full scale.
SATURATION_THRESHOLD = 0.90   # flag if predicted counts exceed 90 % of 65535


# SOLAR GEOMETRY

def day_of_year(d: date) -> int:
    return int(d.strftime("%j"))


def solar_declination_rad(d: date) -> float:
    """Solar declination in radians -- Iqbal/Spencer Fourier series."""
    n = day_of_year(d)
    gamma = 2.0 * np.pi * (n - 1) / 365.0
    return (
        0.006918
        - 0.399912 * np.cos(gamma)    + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2*gamma)  + 0.000907 * np.sin(2*gamma)
        - 0.002697 * np.cos(3*gamma)  + 0.001480 * np.sin(3*gamma)
    )


def calculate_daylight(lat_deg: float, d: date):
    """Returns (hours_of_daylight, condition_string)."""
    phi = np.deg2rad(lat_deg)
    delta = solar_declination_rad(d)
    h0 = np.deg2rad(-0.833)  # refraction + solar disc correction at horizon

    denom = np.cos(phi) * np.cos(delta)
    if abs(denom) < 1e-12:
        # Numerically degenerate near polar boundaries at solstice.
        return (24.0, "Polar Day") if (np.sin(phi) * np.sin(delta) > np.sin(h0)) else (0.0, "Polar Night")

    cos_H0 = (np.sin(h0) - np.sin(phi) * np.sin(delta)) / denom

    if cos_H0 <= -1.0:
        return 24.0, "Polar Day"
    if cos_H0 >= 1.0:
        return 0.0, "Polar Night"

    H0 = np.arccos(cos_H0)
    return float((2.0 * np.rad2deg(H0)) / 15.0), "Normal Day"


def solar_noon_elevation_rad(lat_deg: float, d: date) -> float:
    """Solar elevation angle at true solar noon (radians)."""
    phi = np.deg2rad(lat_deg)
    delta = solar_declination_rad(d)
    sin_elev = np.sin(phi) * np.sin(delta) + np.cos(phi) * np.cos(delta)
    return float(np.arcsin(float(np.clip(sin_elev, -1.0, 1.0))))


def air_mass(elevation_rad: float) -> float:
    """
    Kasten & Young (1989) optical air mass.
    Returns 40 when the sun is at or below the horizon (effectively opaque).
    """
    if elevation_rad <= 0.0:
        return 40.0
    z_deg = 90.0 - np.rad2deg(elevation_rad)
    return 1.0 / (
        np.cos(np.deg2rad(z_deg))
        + 0.50572 * (96.07995 - z_deg) ** -1.6364
    )


# BROADBAND LUX ESTIMATE  (pre-filter, entire solar spectrum)

def estimate_lux(lat_deg: float, d: date, clarity: float = 1.0) -> dict:
    """
    Peak solar-noon illuminance on a horizontal surface, including albedo.

    Parameters
    ----------
    lat_deg  : decimal degrees, +N / -S
    d        : calendar date
    clarity  : 0-1 atmospheric clarity multiplier
               (1.0 = pristine polar air, lower = haze / cloud cover)

    Returns
    -------
    dict with keys: daylight_hours, condition, elevation_deg, air_mass,
                    transmittance, irradiance_Wm2, lux_direct, lux_with_albedo
    """
    hours, condition = calculate_daylight(lat_deg, d)

    if condition == "Polar Night":
        return dict(
            daylight_hours=0.0, condition=condition,
            elevation_deg=0.0, air_mass=40.0,
            transmittance=0.0, irradiance_Wm2=0.0,
            lux_direct=0.0, lux_with_albedo=0.0,
        )

    elev_rad = solar_noon_elevation_rad(lat_deg, d)
    elev_deg = float(np.rad2deg(elev_rad))
    am = air_mass(elev_rad)

    # Beer-Lambert transmittance: T = clarity^AM * exp(-tau * AM)
    transmittance = max(0.0, (clarity ** am) * np.exp(-OPTICAL_DEPTH * am))

    # Horizontal irradiance projected onto flat surface (W m^-2)
    irradiance = SOLAR_CONSTANT * transmittance * max(0.0, float(np.sin(elev_rad)))

    lux_direct = irradiance * LUMINOUS_EFFICACY
    lux_with_albedo = lux_direct * (1.0 + ALBEDO * ALBEDO_BOOST_FACTOR)

    return dict(
        daylight_hours = hours,
        condition = condition,
        elevation_deg = elev_deg,
        air_mass = am,
        transmittance = transmittance,
        irradiance_Wm2 = irradiance,
        lux_direct = lux_direct,
        lux_with_albedo = lux_with_albedo,
    )


# SPECTRAL CORRECTION: BP470 FILTER + TSL2591 SENSOR

def blue_fraction(am: float) -> float:
    """
    Fraction of transmitted solar irradiance in the BP470 passband (425-495 nm).

    Increases with air mass because Rayleigh scattering preferentially removes
    longer wavelengths, enriching the blue component of what remains.
    Hard cap at 0.22 (physically reasonable upper bound for clear sky).
    """
    return min(0.22, SOLAR_BLUE_FRACTION_BASE + RAYLEIGH_BLUE_SLOPE * max(0.0, am - 1.0))


def estimate_filtered_irradiance_uW_cm2(lux_result: dict) -> float:
    """
    Irradiance (uW cm^-2) reaching the TSL2591 photodiode after the BP470 filter,
    accounting for:
        (a) fraction of solar spectrum in 425-495 nm window (air-mass dependent)
        (b) BP470 integrated passband transmission (~0.85)
        (c) albedo contribution from reflected snow
        (d) TSL2591 reduced responsivity at 470 nm vs its calibration peak

    Returns 0.0 during polar night.
    """
    if lux_result['condition'] == "Polar Night":
        return 0.0

    am = lux_result['air_mass']
    bf = blue_fraction(am)
    irr_Wm2 = lux_result['irradiance_Wm2']

    # Direct-beam blue irradiance through filter (W m^-2)
    blue_direct_Wm2 = irr_Wm2 * bf * BP470_INTEGRATED_TRANS

    # Albedo-reflected blue irradiance (snow re-emits blue sky light upward)
    blue_albedo_Wm2 = blue_direct_Wm2 * ALBEDO * ALBEDO_BOOST_FACTOR

    total_blue_Wm2 = blue_direct_Wm2 + blue_albedo_Wm2

    # Apply TSL2591 spectral mismatch correction
    # (sensor under-responds at 470 nm relative to its white-light calibration)
    effective_Wm2 = total_blue_Wm2 * TSL2591_RESPONSIVITY_AT_470NM

    # Convert W m^-2 -> uW cm^-2  (1 W m^-2 = 100 uW cm^-2)
    return effective_Wm2 * 100.0


def irradiance_to_lux_470nm(irradiance_uW_cm2: float) -> float:
    """
    Convert filtered irradiance at the sensor (uW cm^-2) to approximate lux
    using the photopic luminous efficacy at 470 nm.

    This gives the lux value a correctly calibrated photometer would report
    for 470 nm light -- significantly lower than broadband lux because the
    human eye is only ~6 % as sensitive at 470 nm as at 555 nm.
    """
    irradiance_Wm2 = irradiance_uW_cm2 / 100.0   # uW cm^-2 -> W m^-2
    return irradiance_Wm2 * PHOTOPIC_EFFICACY_470NM


# TSL2591 GAIN RECOMMENDATION & RAW COUNT ESTIMATION

def predicted_ch0_counts(irradiance_uW_cm2: float,
                          gain_multiplier: float,
                          integration_s: float = DEFAULT_INTEGRATION_TIME_S) -> float:
    """
    Predicted TSL2591 CH0 ADC counts for a given effective filtered irradiance.

    Formula
    -------
    counts = irradiance (uW cm^-2)
             * responsivity at 470 nm at 1x gain (counts uW^-1 cm^2)
             * gain multiplier
             * (integration_s / 0.100 s)     <- datasheet ref = 100 ms

    Note: irradiance_uW_cm2 here is already corrected for the TSL2591
    spectral response at 470 nm (via estimate_filtered_irradiance_uW_cm2),
    so we use the 470 nm counts-per-uW calibration directly.
    """
    ref_integration_s = 0.100   # datasheet reference integration time

    return (
        irradiance_uW_cm2
        * TSL2591_COUNTS_PER_UW_CM2_1X_470NM
        * gain_multiplier
        * (integration_s / ref_integration_s)
    )


def gain_recommendation(irradiance_uW_cm2: float,
                         integration_s: float = DEFAULT_INTEGRATION_TIME_S) -> dict:
    """
    Recommend the best TSL2591 gain mode for the predicted filtered irradiance.

    Logic
    -----
    Iterate from highest gain (MAX) to lowest (LOW).
    Select the highest gain whose predicted CH0 counts stay below the
    saturation threshold (90 % of 65535 = 58981 counts).

    Returns
    -------
    dict with keys: recommended_gain, gain_multiplier, predicted_counts,
                    saturated, saturation_risk, note
    """
    full_scale = 65535
    sat_limit = int(full_scale * SATURATION_THRESHOLD)  # 58981

    if irradiance_uW_cm2 == 0.0:
        return dict(
            recommended_gain="MAX", gain_multiplier=9876,
            predicted_counts=0, saturated=False,
            saturation_risk="None",
            note="Polar Night -- no light. Use MAX gain for maximum sensitivity.",
        )

    best = None
    for label in ["MAX", "HIGH", "MED", "LOW"]:
        mult, _ = TSL2591_GAINS[label]
        counts = predicted_ch0_counts(irradiance_uW_cm2, mult, integration_s)
        if counts <= sat_limit:
            best = (label, mult, counts)
            break   # highest non-saturating gain found

    if best is None:
        # Even LOW gain (1x) would saturate
        mult = TSL2591_GAINS["LOW"][0]
        counts = predicted_ch0_counts(irradiance_uW_cm2, mult, integration_s)
        return dict(
            recommended_gain="LOW", gain_multiplier=mult,
            predicted_counts=min(int(counts), full_scale),
            saturated=True,
            saturation_risk="OVERRANGE",
            note=(
                "Even LOW gain (1x) saturates at this irradiance. "
                "Consider a neutral-density filter or reducing integration "
                "time to 100 ms."
            ),
        )

    label, mult, counts = best
    counts_int = int(counts)

    # Compute saturation risk level for recommended gain
    ratio = counts_int / full_scale
    if ratio > 0.90:
        risk = "High"
    elif ratio > 0.70:
        risk = "Medium"
    elif ratio > 0.40:
        risk = "Low"
    else:
        risk = "None"

    notes = {
        "MAX":  "Polar twilight / very dim -- MAX gain (9876x) appropriate.",
        "HIGH": "Low light -- HIGH gain (428x) appropriate.",
        "MED":  "Moderate light -- MED gain (25x) recommended.",
        "LOW":  "Bright conditions -- LOW gain (1x) required to prevent saturation.",
    }

    return dict(
        recommended_gain = label,
        gain_multiplier = mult,
        predicted_counts = counts_int,
        saturated = False,
        saturation_risk = risk,
        note = notes[label],
    )


# FULL PIPELINE: SINGLE DATE

def estimate_filtered_sensor(lat_deg: float, d: date,
                              clarity: float = 1.0,
                              integration_s: float = DEFAULT_INTEGRATION_TIME_S
                              ) -> dict:
    """
    Complete model for one date:
      solar geometry -> broadband lux -> BP470 filter -> TSL2591 correction
      -> gain recommendation

    Returns a single merged dict with all intermediate and final values.
    """
    lx = estimate_lux(lat_deg, d, clarity)
    am = lx['air_mass']
    bf = blue_fraction(am)
    irr_uW = estimate_filtered_irradiance_uW_cm2(lx)
    sensor_lux = irradiance_to_lux_470nm(irr_uW)
    gain_info = gain_recommendation(irr_uW, integration_s)

    return dict(
        # Daylight / geometry
        daylight_hours = lx['daylight_hours'],
        condition = lx['condition'],
        elevation_deg = lx['elevation_deg'],
        air_mass = lx['air_mass'],
        transmittance = lx['transmittance'],
        # Broadband
        irradiance_Wm2 = lx['irradiance_Wm2'],
        lux_direct = lx['lux_direct'],
        lux_with_albedo = lx['lux_with_albedo'],
        # Spectral / filter chain
        blue_fraction_pct = round(bf * 100.0, 2),
        filtered_irr_uW_cm2 = round(irr_uW, 5),
        sensor_lux_470nm = round(sensor_lux, 4),
        # TSL2591 gain
        recommended_gain = gain_info['recommended_gain'],
        gain_multiplier = gain_info['gain_multiplier'],
        predicted_ch0_counts = gain_info['predicted_counts'],
        saturated = gain_info['saturated'],
        saturation_risk = gain_info['saturation_risk'],
        gain_note = gain_info['note'],
    )


# UTILITY: CONVERT REAL CH0 READING -> CALIBRATED IRRADIANCE

def ch0_counts_to_irradiance(ch0_counts: int,
                              gain_label: str,
                              integration_s: float = DEFAULT_INTEGRATION_TIME_S
                              ) -> dict:
    """
    Convert a real TSL2591 CH0 raw ADC value to calibrated filtered irradiance
    and approximate 470 nm lux.  Use this on actual field readings.

    Parameters
    ----------
    ch0_counts    : integer raw count read from TSL2591 CH0 register
    gain_label    : one of "LOW", "MED", "HIGH", "MAX"
    integration_s : integration time used (seconds)

    Returns
    -------
    dict with keys: irradiance_uW_cm2, lux_470nm, note
    """
    if gain_label not in TSL2591_GAINS:
        raise ValueError(f"gain_label must be one of {list(TSL2591_GAINS.keys())}")

    gain_mult, full_scale = TSL2591_GAINS[gain_label]
    ref_integration_s = 0.100

    if ch0_counts >= full_scale:
        return dict(
            irradiance_uW_cm2=None,
            lux_470nm=None,
            note="ADC saturated -- reduce gain or integration time.",
        )

    # Invert the forward model directly at 470 nm responsivity.
    effective_uW_cm2 = (
        ch0_counts
        / TSL2591_COUNTS_PER_UW_CM2_1X_470NM
        / gain_mult
        / (integration_s / ref_integration_s)
    )
    lux = irradiance_to_lux_470nm(effective_uW_cm2)

    return dict(
        irradiance_uW_cm2 = round(effective_uW_cm2, 6),
        lux_470nm = round(lux, 5),
        note = "Calibrated for BP470 filter + TSL2591 at 470 nm.",
    )


# CSV GENERATION (full year)

def generate_lookup_table(lat_deg: float, year: int,
                           clarity: float = 1.0,
                           integration_s: float = DEFAULT_INTEGRATION_TIME_S):
    """
    Write one CSV row per day for the full year.
    File is saved in the same directory as this script.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    fname = (
        f"daylight_lux_bp470_{lat_deg:.2f}deg_{year}.csv"
        .replace(" ", "")
    )
    out_path = os.path.join(script_dir, fname)

    headers = [
        "date", "doy", "declination_deg",
        "daylight_hours", "condition",
        "solar_elevation_deg", "air_mass", "transmittance",
        "irradiance_Wm2", "lux_direct", "lux_with_albedo",
        "blue_fraction_pct", "filtered_irr_uW_cm2", "sensor_lux_470nm",
        "recommended_gain", "gain_multiplier",
        "predicted_ch0_counts", "saturated", "saturation_risk",
    ]

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)

        d = date(year, 1, 1)
        end = date(year + 1, 1, 1)
        while d < end:
            r = estimate_filtered_sensor(lat_deg, d, clarity, integration_s)
            decl = float(np.degrees(solar_declination_rad(d)))
            w.writerow([
                d.isoformat(), day_of_year(d), f"{decl:.4f}",
                f"{r['daylight_hours']:.4f}", r['condition'],
                f"{r['elevation_deg']:.3f}", f"{r['air_mass']:.3f}",
                f"{r['transmittance']:.5f}",
                f"{r['irradiance_Wm2']:.2f}",
                f"{r['lux_direct']:.1f}", f"{r['lux_with_albedo']:.1f}",
                f"{r['blue_fraction_pct']:.2f}",
                f"{r['filtered_irr_uW_cm2']:.5f}",
                f"{r['sensor_lux_470nm']:.5f}",
                r['recommended_gain'], r['gain_multiplier'],
                r['predicted_ch0_counts'], r['saturated'], r['saturation_risk'],
            ])
            d += timedelta(days=1)

    print(f"\nCSV saved to:\n{out_path}")


# FORMATTING HELPERS

def fmt_hours(hours: float) -> str:
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h += 1
        m = 0
    return f"{h}h {m}m"


def fmt_lux(lux: float) -> str:
    if lux < 0.001:
        return "0 lx"
    if lux < 1.0:
        return f"{lux * 1000:.2f} mlx"
    if lux >= 1000.0:
        return f"{lux / 1000:.2f} klx"
    return f"{lux:.2f} lx"


def fmt_counts(n: int) -> str:
    return f"{n:,}"


# MAIN

def main():
    SEP = "=" * 54
    SEP2 = "-" * 54

    print(SEP)
    print("  Antarctica Daylight & BP470 / TSL2591 Lux Calculator")
    print(SEP)

    lat_str = input("\nLatitude (degrees, +N / -S, e.g. -75.0): ").strip()
    date_str = input("Date (YYYY-MM-DD): ").strip()
    clar_str = input("Atmospheric clarity 0-1  [Enter = 1.0 pristine]: ").strip()
    intg_str = input("Integration time seconds [Enter = 0.600]: ").strip()

    lat = float(lat_str)
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    clarity = float(clar_str) if clar_str else 1.0
    integration_s = float(intg_str) if intg_str else DEFAULT_INTEGRATION_TIME_S

    r = estimate_filtered_sensor(lat, d, clarity, integration_s)

    print(f"\n{SEP2}")
    print("  SOLAR GEOMETRY")
    print(SEP2)
    print(f"  Latitude          : {lat:.2f} deg")
    print(f"  Date              : {d}  (DOY {day_of_year(d)})")
    print(f"  Condition         : {r['condition']}")
    print(f"  Daylight          : {fmt_hours(r['daylight_hours'])}")
    print(f"  Solar elev (noon) : {r['elevation_deg']:.1f} deg")
    print(f"  Air mass          : {r['air_mass']:.2f}")
    print(f"  Transmittance     : {r['transmittance']:.4f}")

    print(f"\n{SEP2}")
    print("  BROADBAND  (full solar spectrum, unfiltered)")
    print(SEP2)
    print(f"  Irradiance        : {r['irradiance_Wm2']:.1f} W m^-2")
    print(f"  Lux (direct sun)  : {fmt_lux(r['lux_direct'])}")
    print(f"  Lux (+ albedo)    : {fmt_lux(r['lux_with_albedo'])}")

    print(f"\n{SEP2}")
    print("  BP470 FILTER + TSL2591 SPECTRAL CORRECTION")
    print(SEP2)
    print(f"  Blue fraction     : {r['blue_fraction_pct']:.2f} %  (425-495 nm)")
    print(f"  Filtered irrad.   : {r['filtered_irr_uW_cm2']:.5f} uW cm^-2  at sensor")
    print(f"  Sensor lux @470nm : {fmt_lux(r['sensor_lux_470nm'])}")
    print(f"  (This is ~6 % of broadband lux -- eye has low sensitivity at 470 nm)")

    print(f"\n{SEP2}")
    print(f"  TSL2591 GAIN RECOMMENDATION  (integration = {integration_s*1000:.0f} ms)")
    print(SEP2)
    print(f"  Recommended gain  : {r['recommended_gain']}  ({r['gain_multiplier']}x)")
    print(f"  Predicted CH0     : {fmt_counts(r['predicted_ch0_counts'])} counts")
    print(f"  Saturation risk   : {r['saturation_risk']}")
    print(f"  Note: {r['gain_note']}")

    if r['saturated']:
        print()
        print("  *** WARNING: SENSOR WILL SATURATE ON THIS DAY ***")
        print("      Reduce gain or shorten integration time.")

    # -- Optional: convert a real CH0 reading --------------------------------
    print(f"\n{SEP2}")
    ans = input("Convert a real CH0 reading to irradiance? (y/n): ").strip().lower()
    if ans == "y":
        ch0_str = input("  CH0 raw count (integer): ").strip()
        gain_str = input("  Gain used (LOW / MED / HIGH / MAX): ").strip().upper()
        cal = ch0_counts_to_irradiance(int(ch0_str), gain_str, integration_s)
        if cal['irradiance_uW_cm2'] is None:
            print(f"  {cal['note']}")
        else:
            print(f"  Irradiance  : {cal['irradiance_uW_cm2']:.6f} uW cm^-2")
            print(f"  Lux @470 nm : {fmt_lux(cal['lux_470nm'])}")

    # -- Full-year CSV --------------------------------------------------------
    print(f"\n{SEP2}")
    ans = input("Save full-year CSV lookup table? (y/n): ").strip().lower()
    if ans == "y":
        generate_lookup_table(lat, d.year, clarity, integration_s)

    print(f"\n{SEP}")
    print("  Done.")
    print(SEP)


if __name__ == "__main__":
    main()