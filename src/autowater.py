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

max_watering_time = 25  # seconds
log_file_path = "logs/autowater.log"

#
logger.add(log_file_path, rotation="1 month", retention="3 years", compression="zip")


def check_in_to_snitch(snitch_url):
    try:
        response = requests.get(snitch_url)
        response.raise_for_status()  # this will raise an exception for HTTP errors
        logger.info(f"Checked in to snitch: {snitch_url}")
    except Exception as e:
        # Handle or log the exception as appropriate
        pass


# Then, periodically call this function from your main process or a scheduled task.


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

    # get the access token from the file ../token.json
    access_token = project_settings["access_token"]

    assert access_token, "Access token is empty"

    # Setup the headers with the Access Token
    headers = {
        "Authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }

    sensor_df = None

    # Specify the time period for the history
    # Here we're getting the history for the last day
    start_time = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
    end_time = datetime.datetime.now().isoformat()

    # we want to loop this process for each plant in the plant_dict
    for plant_name, plant_data in plant_dict.items():
        # Making the request to the Home Assistant API
        response = requests.get(
            f"{home_assistant_url}/api/history/period/{start_time}",
            headers=headers,
            params={"filter_entity_id": plant_data["sensor"], "end_time": end_time},
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

        # convert the plant_name column to int
        history_df[plant_name] = history_df[plant_name].astype(int)

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
            max_watering_time,
        )  # For example, no negative watering time

        # set the integral and last error
        if clear_pid_history:
            pid_controllers[plant]._integral = 0
            pid_controllers[plant]._last_error = 0
        else:
            pid_controllers[plant]._integral = plant_config["integral"]
            pid_controllers[plant]._last_error = plant_config["last_error"]

    # Iterate over the DataFrame's columns
    for plant, series in sensor_df.items():
        if plant in pid_controllers:
            # Drop NaNs and compute the error
            non_nan_values = series.dropna()
            if not non_nan_values.empty:
                # Get the latest moisture level
                current_moisture_level = non_nan_values.iloc[-1]

                # if the plant is at 40% or greater, don't water until it's at 34%
                if current_moisture_level >= 40:
                    plant_dict[plant]["resting"] = True
                    logger.info(
                        f"Plant {plant} is resting with moisture level {current_moisture_level}"
                    )
                    continue

                # check to see if the plant is resting
                if plant_dict[plant]["resting"]:
                    if current_moisture_level <= 34:
                        plant_dict[plant]["resting"] = False
                    else:
                        logger.info(
                            f"Plant {plant} is resting with moisture level {current_moisture_level}"
                        )
                        continue

                pid = pid_controllers[plant]

                # Compute the control variable (how long to water)
                control = pid(current_moisture_level)

                # store the integral and last error in the plant_dict dictionary
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
        with open("plant_data.json", "w") as f:
            json.dump(plant_dict, f, indent=4)

    check_in_to_snitch(project_settings["snitch_url"])


if __name__ == "__main__":
    main()
