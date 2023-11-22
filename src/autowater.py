import pyrootutils

root = pyrootutils.setup_root(
    search_from=__file__,
    indicator=[".git", "pyproject.toml"],
    pythonpath=True,
    dotenv=True,
)

# ------------------------------------------------------------------------------------ #
# `pyrootutils.setup_root(...)` above is optional line to make environment more convenient
# should be placed at the top of each entry file
#
# main advantages:
# - allows you to keep all entry files in "src/" without installing project as a package
# - launching python file works no matter where is your current work dir
# - automatically loads environment variables from ".env" if exists
#
# how it works:
# - `setup_root()` above recursively searches for either ".git" or "pyproject.toml" in present
#   and parent dirs, to determine the project root dir
# - adds root dir to the PYTHONPATH (if `pythonpath=True`), so this file can be run from
#   any place without installing project as a package
# - sets PROJECT_ROOT environment variable which is used in "configs/paths/default.yaml"
#   to make all paths always relative to project root
# - loads environment variables from ".env" in root dir (if `dotenv=True`)
#
# you can remove `pyrootutils.setup_root(...)` if you:
# 1. either install project as a package or move each entry file to the project root dir
# 2. remove PROJECT_ROOT variable from paths in "configs/paths/default.yaml"
#
# https://github.com/ashleve/pyrootutils
# ------------------------------------------------------------------------------------ #

import datetime
import json
from time import sleep

import click
import pandas as pd
import requests
import tinytuya
from loguru import logger
from simple_pid import PID

log_file_path = "logs/autowater.log"

logger.add(log_file_path, rotation="1 month", retention="3 years", compression="zip")


def check_in_to_snitch(snitch_url):
    try:
        response = requests.get(snitch_url)
        response.raise_for_status()  # this will raise an exception for HTTP errors
        logger.info(f"Checked in to snitch: {snitch_url}")
    except Exception as e:
        # Handle or log the exception as appropriate
        pass


def send_data_to_home_assistant(
    value, sensor_name, home_assistant_url, headers, units=None
):
    """Sends data to Home Assistant.

    Args:
        value (float): The value to send to Home Assistant.
        sensor_name (str): The name of the sensor in Home Assistant.
        home_assistant_url (str): The URL of the Home Assistant instance.
        headers (str): The headers for the Home Assistant API.
        units (str, optional): The units of the value. Defaults to None.
    """
    if units:
        data = {
            "state": value,
            "attributes": {
                "unit_of_measurement": units,
            },
        }
    else:
        data = {"state": value}
    response = requests.post(
        f"{home_assistant_url}/api/states/sensor.{sensor_name}",
        headers=headers,
        json=data,
    )
    if response.status_code == 200:
        logger.debug(f"Successfully updated {sensor_name} in Home Assistant")
    elif response.status_code == 201:
        logger.debug(f"Successfully created {sensor_name} in Home Assistant")
    else:
        logger.error(
            f"Failed to update {sensor_name}: {response.content} in Home Assistant"
        )


def get_data_from_home_assistant(
    plant_dict: dict,
    sensor: str,
    home_assistant_url: str,
    headers: str,
):
    """Gets the data from Home Assistant and returns a DataFrame.

    Args:
        plant_dict (dict): A dictionary containing the plant settings.
        sensor (str): The name of the sensor in Home Assistant.
        home_assistant_url (str): The URL of the Home Assistant instance.
        headers (str): The headers for the Home Assistant API.
        df (pd.DataFrame, optional): A DataFrame to merge the data into. Defaults to None.
    """
    # Initialize the DataFrame
    sensor_df = None

    # Specify the time period for the history
    # Here we're getting the history for the last day
    start_time = (datetime.datetime.now() - datetime.timedelta(days=10)).isoformat()
    end_time = datetime.datetime.now().isoformat()

    # we want to loop this process for each plant in the plant_dict
    for plant_name, plant_data in plant_dict.items():
        # Making the request to the Home Assistant API
        response = requests.get(
            f"{home_assistant_url}/api/history/period/{start_time}",
            headers=headers,
            params={
                "filter_entity_id": f"sensor.{plant_data[sensor]}",
                "end_time": end_time,
            },
        )

        # Check for successful response
        if response.status_code == 200:
            history_data = response.json()
            # logger.info(json.dumps(history_data, indent=2))
        else:
            logger.error(
                f"Failed to retrieve history: {response.status_code}, {response.text}"
            )

        # Parse the history data into a pandas DataFrame
        history_list = []
        for state_info in history_data[
            0
        ]:  # Assuming the sensor's history is the first item
            # If state is 'unavailable'
            if state_info["state"] != "unavailable":
                history_list.append(
                    {
                        "time": state_info["last_updated"],
                        plant_name: state_info["state"],
                    }
                )

        # Convert the list to a DataFrame
        history_df = pd.DataFrame(history_list)

        # Convert 'time' to datetime type
        history_df["time"] = pd.to_datetime(history_df["time"])

        # round the time to the nearest minute
        history_df["time"] = history_df["time"].dt.round("min")

        # remove rows with text
        history_df[plant_name] = pd.to_numeric(history_df[plant_name], errors="coerce")

        # remove rows with NaN
        history_df.dropna(inplace=True)

        # if the sensor_df is empty, set it equal to the history_df
        if sensor_df is None:
            sensor_df = history_df
        else:
            # merge the history_df with the sensor_df
            sensor_df = sensor_df.merge(history_df, on="time", how="outer")

    # set the index to the time column
    sensor_df.set_index("time", inplace=True)

    # sort the index
    sensor_df.sort_index(inplace=True)

    return sensor_df


def water(plant, dev_id, ip, local_key, seconds):
    """Used to water a plant for a specified number of seconds.

    Designed to work with the RainPoint smart water pump.

    Args:
        plant (str): The name of the plant being watered.
        dev_id (str): The device ID of the water pump.
        ip (str): The IP address of the water pump.
        local_key (str): The local key of the water pump.
        seconds (int): The number of seconds to water the plant.
    """
    device = tinytuya.Device(
        dev_id=dev_id, address=ip, local_key=local_key, version="3.3"
    )

    # Helper function to safely execute device commands
    def execute_device_command(command_function):
        try:
            result = command_function()
            if "Error" in result:
                logger.error(f"Error communicating with the device: {result['Error']}")
                return False
            return True
        except Exception as e:
            logger.error(f"Exception occurred: {e}")
            return False

    # Check device status safely
    if not execute_device_command(device.status):
        return

    # Attempt to turn on the device safely
    if not execute_device_command(device.turn_on):
        # If turning on fails, no need to continue
        return

    # Wait for the specified time
    sleep(seconds)

    # Attempt to turn off the device safely
    for _ in range(3):  # Try to turn off the device up to 3 times
        if execute_device_command(device.turn_off):
            logger.info(
                f"{plant} has been watered for {seconds} seconds and turned off successfully."
            )
            break
        sleep(1)  # Wait a bit before retrying
    else:
        logger.error(
            f"Failed to turn off {plant} after watering. Manual intervention may be required."
        )


@click.command()
@click.option(
    "--clear-pid-history",
    is_flag=True,
    default=False,
    required=False,
    help="Device ID",
    type=bool,
)
@click.option(
    "--dont-water",
    required=False,
    is_flag=True,
    default=False,
    help="Will not water or make changes to the plant settings file",
    type=bool,
)
def main(
    dont_water: bool = False,
    clear_pid_history: bool = False,
):
    """Main entry point for the autowater script.

    Args:
        dont_water (bool, optional): Will skip watering the plants and won't make changes to the plant settings file. Defaults to False.
        clear_pid_history (bool, optional): Clears the PID history. Defaults to False.
    """
    # import the pump_list.json file as a dictionary
    plant_dict: dict = json.load(open("plant_settings.json"))
    project_settings = json.load(open("project_settings.json"))

    # Configuration
    home_assistant_url = f"{project_settings['home_assistant_url']}:{project_settings['home_assistant_port']}"

    # get the access token from the file
    access_token = project_settings["access_token"]

    assert access_token, "Access token is empty"

    # Setup the headers with the Access Token
    headers = {
        "Authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }

    # Get the moisture sensor data from Home Assistant
    moisture_sensor_df = get_data_from_home_assistant(
        plant_dict=plant_dict,
        sensor="moisture_sensor",
        home_assistant_url=home_assistant_url,
        headers=headers,
    )

    # Get the previous watering time from Home Assistant
    pump_sensor_df = get_data_from_home_assistant(
        plant_dict=plant_dict,
        sensor="pump_sensor",
        home_assistant_url=home_assistant_url,
        headers=headers,
    )

    pid_controllers = {}
    for plant, plant_config in plant_dict.items():
        pid_controllers[plant]: PID = PID(
            Kp=plant_config["Kp"],
            Ki=plant_config["Ki"],
            Kd=plant_config["Kd"],
            setpoint=plant_config["target"],
        )

        # Set output limits if necessary
        pid_controllers[plant].output_limits = (
            0,
            plant_config["max_watering_time"],
        )  # in seconds

        # set the integral and last error
        if clear_pid_history:
            pid_controllers[plant]._integral = 0
            pid_controllers[plant]._last_error = 0
        else:
            pid_controllers[plant]._integral = plant_config["integral"]
            pid_controllers[plant]._last_error = plant_config["last_error"]

    # Iterate over the DataFrame's columns
    for plant, series in moisture_sensor_df.items():
        if plant in pid_controllers:
            # Drop NaNs and compute the error
            non_nan_values = series.dropna()
            if not non_nan_values.empty:
                # Get the latest moisture level
                current_moisture_level = non_nan_values.iloc[-1]

                # check to see if the plant is resting
                if plant_dict[plant]["resting"]:
                    # if so, check to see if the moisture level is below the threshold
                    if current_moisture_level <= plant_dict[plant]["resting_target"]:
                        # if so, set the resting flag to False
                        plant_dict[plant]["resting"] = False
                        logger.info(
                            f"Plant {plant} is no longer resting. Resuming watering."
                        )

                        # send the resting data to Home Assistant
                        send_data_to_home_assistant(
                            value="off",
                            sensor_name=plant_dict[plant]["rest_sensor"],
                            home_assistant_url=home_assistant_url,
                            headers=headers,
                        )
                    else:
                        # if not, skip the plant
                        logger.info(
                            f"Plant {plant} is still resting with moisture level {current_moisture_level} and resting target {plant_dict[plant]['resting_target']}"
                        )
                        continue
                else:
                    # check to see if the plant should be resting
                    if plant_dict[plant]["resting_target"]:
                        # if a resting target is specified, check to see if the moisture level is above the target
                        if current_moisture_level >= plant_dict[plant]["target"]:
                            # if so, set the resting flag to True
                            plant_dict[plant]["resting"] = True
                            logger.info(
                                f"Plant {plant} has begun resting with moisture level {current_moisture_level}"
                            )

                            # send the resting data to Home Assistant
                            send_data_to_home_assistant(
                                value="on",
                                sensor_name=plant_dict[plant]["rest_sensor"],
                                home_assistant_url=home_assistant_url,
                                headers=headers,
                            )

                        # if not, skip the plant
                        logger.info(
                            f"Plant {plant} is resting with moisture level {current_moisture_level}"
                        )
                        continue

                # Get the last watering time
                prior_watering_length = pump_sensor_df[plant].dropna().iloc[-1]

                # Grab the PID controller for the plant
                pid = pid_controllers[plant]

                # Compute the control variable (how long to water)
                control = pid(current_moisture_level)

                # Check to see if the control variable is the same as the last watering time
                if (control == prior_watering_length) and (control > 0):
                    # if so, we want to change it by a small amount
                    # this is because Home Assistant will not update the sensor history if the value is the same
                    # so we need to change it by a small amount to trigger the update
                    control += 0.01

                # Send the data to Home Assistant
                if plant_dict[plant]["pump_sensor"] and not dont_water:
                    send_data_to_home_assistant(
                        value=control,
                        sensor_name=plant_dict[plant]["pump_sensor"],
                        home_assistant_url=home_assistant_url,
                        headers=headers,
                    )

                # store the integral and last error in the plant_dict dictionary
                if not dont_water:
                    plant_dict[plant]["integral"] = pid._integral
                    plant_dict[plant]["last_error"] = pid._last_error

                # Decide if we need to water the plant
                if control > 0:
                    # Call your water function with the appropriate parameters
                    # water(plant, "ip_placeholder", "local_key_placeholder", control)
                    logger.info(
                        f"Watering {plant} with moisture level {current_moisture_level} for {control} seconds"
                    )
                    if not dont_water:
                        water(
                            plant=plant,
                            dev_id=plant_dict[plant]["id"],
                            ip=plant_dict[plant]["ip"],
                            local_key=plant_dict[plant]["local_key"],
                            seconds=control,
                        )
                else:
                    logger.info(
                        f"Plant {plant} is good with moisture level {current_moisture_level}, control: {control}"
                    )
        else:
            logger.error(f"No PID controller for {plant}")

    # write out the plant_dict to a json file
    if not dont_water:
        with open("plant_settings.json", "w") as f:
            json.dump(plant_dict, f, indent=4)

    if "snitch_url" in project_settings:
        check_in_to_snitch(snitch_url=project_settings["snitch_url"])


if __name__ == "__main__":
    main()
