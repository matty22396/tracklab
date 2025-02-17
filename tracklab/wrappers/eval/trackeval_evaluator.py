import json
import os
import zipfile

import numpy as np
import pandas as pd
import logging
import trackeval

from pathlib import Path
from tabulate import tabulate
from tracklab.core import Evaluator as EvaluatorBase
from tracklab.utils import wandb

log = logging.getLogger(__name__)


class TrackEvalEvaluator(EvaluatorBase):
    """
    Evaluator using the TrackEval library (https://github.com/JonathonLuiten/TrackEval).
    Save on disk the tracking predictions and ground truth in MOT Challenge format and run the evaluation by calling TrackEval.
    """
    def __init__(self, cfg, eval_set, trackeval_dataset_class, show_progressbar, dataset_path, *args, **kwargs):
        self.cfg = cfg
        self.eval_set = eval_set
        self.trackeval_dataset_name = trackeval_dataset_class
        self.trackeval_dataset_class = getattr(trackeval.datasets, trackeval_dataset_class)
        self.show_progressbar = show_progressbar
        self.dataset_path = dataset_path

    def run(self, tracker_state):
        log.info("Starting evaluation using TrackEval library (https://github.com/JonathonLuiten/TrackEval)")

        tracker_name = 'tracklab'
        save_classes = self.trackeval_dataset_class.__name__ != 'MotChallenge2DBox'

        # Save predictions
        pred_save_path = Path(self.cfg.dataset.TRACKERS_FOLDER) / f"{self.trackeval_dataset_class.__name__}-{self.eval_set}" / tracker_name
        save_functions[self.trackeval_dataset_name](
            tracker_state.detections_pred,
            tracker_state.image_metadatas,
            tracker_state.video_metadatas,
            pred_save_path,
            self.cfg.bbox_column_for_eval,
            save_classes,  # do not use classes for MOTChallenge2DBox
            is_ground_truth=False,
        )

        log.info(
            f"Tracking predictions saved in {self.trackeval_dataset_name} format in {pred_save_path}")

        if tracker_state.detections_gt is None or len(tracker_state.detections_gt) == 0:
            log.warning(
                f"Stopping evaluation because the current split ({self.eval_set}) has no ground truth detections.")
            return

        # Save ground truth
        if self.cfg.save_gt:
            save_functions[self.trackeval_dataset_name](
                tracker_state.detections_gt,
                tracker_state.image_metadatas,
                tracker_state.video_metadatas,
                Path(self.cfg.dataset.GT_FOLDER) / f"{self.trackeval_dataset_name}-{self.eval_set}",
                self.cfg.bbox_column_for_eval,
                save_classes,
                is_ground_truth=True
            )

        log.info(
            f"Tracking ground truth saved in {self.trackeval_dataset_name} format in {pred_save_path}")

        # Build TrackEval dataset
        dataset_config = self.trackeval_dataset_class.get_default_dataset_config()
        dataset_config['SEQ_INFO'] = tracker_state.video_metadatas.set_index('name')['nframes'].to_dict()
        dataset_config['BENCHMARK'] = self.trackeval_dataset_class.__name__  # required for trackeval.datasets.MotChallenge2DBox
        for key, value in self.cfg.dataset.items():
            dataset_config[key] = value

        if not self.cfg.save_gt:
            dataset_config['GT_FOLDER'] = self.dataset_path  # Location of GT data
            dataset_config['GT_LOC_FORMAT'] = '{gt_folder}/{seq}/Labels-GameState.json'  # '{gt_folder}/{seq}/gt/gt.txt'
        dataset = self.trackeval_dataset_class(dataset_config)

        # Build metrics
        metrics_config = {'METRICS': set(self.cfg.metrics), 'PRINT_CONFIG': False, 'THRESHOLD': 0.5}
        metrics_list = []
        for metric_name in self.cfg.metrics:
            try:
                metric = getattr(trackeval.metrics, metric_name)
                metrics_list.append(metric(metrics_config))
            except AttributeError:
                log.warning(f'Skipping evaluation for unknown metric: {metric_name}')

        # Build evaluator
        eval_config = trackeval.Evaluator.get_default_eval_config()
        for key, value in self.cfg.eval.items():
            eval_config[key] = value
        evaluator = trackeval.Evaluator(eval_config)

        # Run evaluation
        output_res, output_msg = evaluator.evaluate([dataset], metrics_list, show_progressbar=self.show_progressbar)

        # Log results
        results = output_res[dataset.get_name()][tracker_name]
        combined_results = results.pop('SUMMARIES')
        wandb.log(combined_results)


def save_in_soccernet_format(detections: pd.DataFrame,
                             image_metadatas: pd.DataFrame,
                             video_metadatas: pd.DataFrame,
                             save_folder: str,
                             bbox_column_for_eval="bbox_ltwh",
                             save_classes=False,
                             is_ground_truth=False,
                             save_zip=True
                             ):
    if is_ground_truth:
        return
    save_path = Path(save_folder)
    save_path.mkdir(parents=True, exist_ok=True)
    detections = soccernet_encoding(detections.copy(), supercategory="object")
    camera_metadata = soccernet_encoding(image_metadatas.copy(), supercategory="camera")
    pitch_metadata = soccernet_encoding(image_metadatas.copy(), supercategory="pitch")
    predictions = pd.concat([detections, camera_metadata, pitch_metadata], ignore_index=True)
    zf_save_path = save_path.parents[1] / f"{save_path.parent.name}.zip"
    for id, video in video_metadatas.iterrows():
        file_path = save_path / f"{video['name']}.json"
        video_predictions_df = predictions[predictions["video_id"] == str(id)].copy()
        if not video_predictions_df.empty:
            video_predictions_df.sort_values(by="id", inplace=True)
            video_predictions = [{k: int(v) if k == 'track_id' else v for k, v in m.items() if np.all(pd.notna(v))} for m in video_predictions_df.to_dict(orient="records")]
            with file_path.open("w") as fp:
                json.dump({"predictions": video_predictions}, fp, indent=2)
            if save_zip:
                with zipfile.ZipFile(zf_save_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(file_path, arcname=f"{save_path.name}/{file_path.name}")


def transform_bbox_image(row):
    return {"x": row[0], "y": row[1], "w": row[2], "h": row[3]}

def soccernet_encoding(dataframe: pd.DataFrame, supercategory):
    dataframe["supercategory"] = supercategory
    dataframe = dataframe.map(lambda x: x.tolist() if isinstance(x, np.ndarray) else x)
    dataframe = dataframe.replace({np.nan: None})
    if supercategory == "object":
        # Remove detections that don't have mandatory columns
        # Detections with no track_id will therefore be removed and not count as FP at evaluation
        dataframe.dropna(
            subset=[
                "track_id",
                "bbox_ltwh",
                "bbox_pitch",
            ],
            how="any",
            inplace=True,
        )
        dataframe = dataframe.rename(columns={"bbox_ltwh": "bbox_image", "jersey_number": "jersey"})
        dataframe["track_id"] = dataframe["track_id"]
        dataframe["attributes"] = dataframe.apply(
            lambda x: x[x.index.intersection(["role", "jersey", "team"])].to_dict(),
            axis=1
        )
        dataframe["id"] = dataframe.index
        dataframe = dataframe[dataframe.columns.intersection(
            ["id", "image_id", "video_id", "track_id", "supercategory",
             "category_id", "attributes", "bbox_image", "bbox_pitch"])]

        dataframe['bbox_image'] = dataframe['bbox_image'].apply(transform_bbox_image)
    elif supercategory == "camera":
        dataframe["image_id"] = dataframe.index
        dataframe["category_id"] = 6
        dataframe["id"] = dataframe.index.map(lambda x: str(x) + "01")
        dataframe = dataframe[dataframe.columns.intersection(
            ["id", "image_id", "video_id", "supercategory", "category_id", "parameters",
             "relative_mean_reproj", "accuracy@5"])
        ]
    elif supercategory == "pitch":
        dataframe["image_id"] = dataframe.index
        dataframe["category_id"] = 5
        dataframe["id"] = dataframe.index.map(lambda x: str(x) + "00")
        dataframe = dataframe[dataframe.columns.intersection(
            ["id", "image_id", "video_id", "supercategory", "category_id", "lines"])]
    dataframe["video_id"] = dataframe["video_id"].apply(str)
    dataframe["image_id"] = dataframe["image_id"].apply(str)
    dataframe["id"] = dataframe["id"].apply(str)
    return dataframe


def save_in_mot_challenge_format(detections, image_metadatas, video_metadatas,
                                 save_folder, bbox_column_for_eval="bbox_ltwh",
                                 save_classes=False, is_ground_truth=False):
    mot_df = _mot_encoding(detections, image_metadatas, video_metadatas, bbox_column_for_eval)

    save_path = os.path.join(save_folder)
    os.makedirs(save_path, exist_ok=True)

    # MOT Challenge format = <frame>, <id>, <bb_left>, <bb_top>, <bb_width>, <bb_height>, <conf>, <x>, <y>, <z>
    # videos_names = mot_df["video_name"].unique()
    for id, video in video_metadatas.iterrows():
        file_path = os.path.join(save_path, f"{video['name']}.txt")
        file_df = mot_df[mot_df["video_id"] == id].copy()
        if file_df["frame"].min() == 0:
            file_df["frame"] = file_df["frame"] + 1  # MOT Challenge format starts at 1
        if not file_df.empty:
            file_df.sort_values(by="frame", inplace=True)
            clazz = "category_id" if save_classes else "x"
            file_df[
                [
                    "frame",
                    "track_id",
                    "bb_left",
                    "bb_top",
                    "bb_width",
                    "bb_height",
                    "bbox_conf",
                    clazz,
                    "y",
                    "z",
                ]
            ].to_csv(
                file_path,
                header=False,
                index=False,
            )
        else:
            open(file_path, "w").close()


def _mot_encoding(detections, image_metadatas, video_metadatas, bbox_column):
    detections = detections.copy()
    image_metadatas["id"] = image_metadatas.index
    df = pd.merge(
        image_metadatas.reset_index(drop=True),
        detections.reset_index(drop=True),
        left_on="id",
        right_on="image_id",
        suffixes=('', '_y')
    )
    len_before_drop = len(df)
    df.dropna(
        subset=[
            "frame",
            "track_id",
            bbox_column,
        ],
        how="any",
        inplace=True,
    )

    if len_before_drop != len(df):
        log.warning(
            "Dropped {} rows with NA values".format(len_before_drop - len(df))
        )
    df["track_id"] = df["track_id"].astype(int)
    df["bb_left"] = df[bbox_column].apply(lambda x: x[0])
    df["bb_top"] = df[bbox_column].apply(lambda x: x[1])
    df["bb_width"] = df[bbox_column].apply(lambda x: x[2])
    df["bb_height"] = df[bbox_column].apply(lambda x: x[3])
    df = df.assign(x=-1, y=-1, z=-1)
    return df


def _print_results(
    res_combined,
    res_by_video=None,
    scale_factor=1.0,
    title="",
    print_by_video=False,
):
    headers = res_combined.keys()
    data = [
        format_metric(name, res_combined[name], scale_factor)
        for name in headers
    ]
    log.info(f"{title}\n" + tabulate([data], headers=headers, tablefmt="plain"))
    if print_by_video and res_by_video:
        data = []
        for video_name, res in res_by_video.items():
            video_data = [video_name] + [
                format_metric(name, res[name], scale_factor)
                for name in headers
            ]
            data.append(video_data)
        headers = ["video"] + list(headers)
        log.info(
            f"{title} by videos\n"
            + tabulate(data, headers=headers, tablefmt="plain")
        )


def format_metric(metric_name, metric_value, scale_factor):
    if (
        "TP" in metric_name
        or "FN" in metric_name
        or "FP" in metric_name
        or "TN" in metric_name
    ):
        if metric_name == "MOTP":
            return np.around(metric_value * scale_factor, 3)
        return int(metric_value)
    else:
        return np.around(metric_value * scale_factor, 3)


save_functions = {
    "MotChallenge2DBox": save_in_mot_challenge_format,
    "SoccerNetGS": save_in_soccernet_format,
}
