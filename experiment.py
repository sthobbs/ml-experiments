
import pandas as pd
import dask.dataframe as dd
import xgboost as xgb
from sklearn import ensemble, tree, neural_network, neighbors, linear_model, cluster, base
from sklearn.utils import shuffle
from sklearn.model_selection import ParameterGrid
from sklearn.inspection import permutation_importance
import shap
import numpy as np
from pathlib import Path
import yaml, pickle, os, time, random
from datetime import datetime
from shutil import copyfile
import matplotlib.pyplot as plt
from tqdm import tqdm
from hyperopt import fmin, rand, tpe, atpe, hp, STATUS_OK, Trials, pyll
import warnings
warnings.filterwarnings('ignore')

pwd = r"C:\Users\hobbs\Documents\Programming\ML"
os.chdir(pwd)
from model_evaluation import ModelEvaluation, metric_score



class ConfigError(Exception):
    """Exception for issues with a configuration file."""
    
    pass


class Experiment():
    """Class for training and evaluating ML models"""
    
    def __init__(self, config_path):
        """
        Constructs attributes from a config file

        Parameters
        ----------
            config_path : str
                path to yaml config file
        """

        print(f"\n-----Initializing {self.__class__.__name__}-----")

        # Load and validate config file
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        self.validate_config()
        
        # Set variables
        self.config_path = Path(config_path)
        self.version = self.config["version"]
        self.data_dir = Path(self.config["data_dir"])
        self.data_file_patterns = self.config["data_file_patterns"]
        self.input_model_path = self.config["input_model_path"]
        self.experiment_dir = Path(self.config["experiment_dir"])
        now = datetime.now().strftime("%Y%m%d-%H%M%S") # current datetime
        self.output_dir = self.experiment_dir / f"{self.version}-{now}"
        self.performance_dir = self.output_dir / self.config["performance_dir"]
        self.model_dir = self.output_dir / self.config["model_dir"]
        self.explain_dir = self.output_dir / self.config["explain_dir"]
        self.save_scores = self.config["save_scores"]
        if self.save_scores:
            self.score_dir = self.output_dir / self.config["score_dir"]
        self.model_type = self.config["model_type"]
        self.supervised = self.config["supervised"]
        self.binary_classification = self.config["binary_classification"]
        self.label = None
        if self.supervised:
            self.label = self.config["label"]
        self.features = self.config["features"]
        self.aux_fields = self.config.get("aux_fields", None)
        if self.aux_fields is None:
            self.aux_fields = []
        elif isinstance(self.aux_fields, str):
            self.aux_fields = [self.aux_fields]
        assert isinstance(self.aux_fields, list), \
            f"self.aux_fields must be a list or string, not {type(self.aux_fields)}"
        self.seed = int(self.config["seed"])
        random.seed(self.seed)
        np.random.seed(self.seed)
        self.hyperparameters = self.config["hyperparameters"]
        self.hyperparameters["random_state"] = self.seed
        self.hyperparameter_tuning = self.config["hyperparameter_tuning"]
        self.hyperparameter_eval_metric = self.config.get("hyperparameter_eval_metric", None)
        self.tuning_algorithm = self.config.get("tuning_algorithm", None)
        self.tuning_iterations = self.config.get("tuning_iterations", None)
        self.tuning_parameters = self.config.get("tuning_parameters", None)
        self.permutation_importance = self.config.get("permutation_importance", False)
        self.perm_imp_metrics = self.config.get("perm_imp_metrics", "neg_log_loss")
        if isinstance(self.perm_imp_metrics, str):
            self.perm_imp_metrics = [self.perm_imp_metrics]
        self.perm_imp_n_repeats = int(self.config.get("perm_imp_n_repeats", 10))
        self.shap = self.config.get("shap", False)
        self.shap_sample = self.config.get("shap_sample", None)
        self.data = {} # where data will be stored
        self.aux_data = {} # where auxiliary fields will be stored
        self.model = None
        
        # custom order dataset_names, so validation is last since 
        # the last dataset is used for early stopping (if enabled)
        all_names = set(self.data_file_patterns)
        main_names = {'train', 'test', 'validation'}
        other_names = sorted(all_names.difference(main_names))
        self.dataset_names = ['train', 'test'] + other_names + ['validation']

    def validate_config(self):
        """Ensure that the config file is valid."""

        # specify which keys and values are required/valid
        required_keys = {
            'version', 'description', 'data_dir', 'data_file_patterns', 
            'input_model_path', 'experiment_dir', 'performance_dir', 
            'model_dir', 'explain_dir', 'save_scores', 'model_type',
            'supervised', 'binary_classification', 'features', 'aux_fields',
            'seed', 'hyperparameters', 'hyperparameter_tuning'
        }
        other_valid_keys = {
            'score_dir', 'label', 'verbose', 'hyperparameter_eval_metric',
            'tuning_algorithm', 'tuning_iterations', 'tuning_parameters',
            'permutation_importance', 'perm_imp_metrics', 'perm_imp_n_repeats',
            'shap', 'shap_sample'
        }
        valid_keys = required_keys.union(other_valid_keys)
        keys_with_required_vals = {
            'version', 'description', 'data_dir', 'data_file_patterns',
            'experiment_dir', 'performance_dir', 'model_dir', 'explain_dir',
            'save_scores', 'model_type', 'supervised', 'features',
            'seed', 'hyperparameter_tuning'
        }
        keys = set(self.config.keys())
        keys_with_vals = {k for k, v in self.config.items() if v is not None}
        
        # check for missing required keys
        missing_required_keys = required_keys.difference(keys)
        if missing_required_keys:
            msg = f"missing key(s) in config file: {', '.join(missing_required_keys)}"
            raise ConfigError(msg)
        
        # check for non-valid keys
        unexpected_keys = keys.difference(valid_keys)
        if unexpected_keys:
            msg = f"unexpected key(s) in config file: {', '.join(unexpected_keys)}"
            raise ConfigError(msg)
        
        # check for keys with missing required values
        keys_missing_required_vals = keys_with_required_vals.difference(keys_with_vals)
        if keys_missing_required_vals:
            msg = f"missing value(s) in config file for: {', '.join(keys_missing_required_vals)}"
            raise ConfigError(msg)
        
        # check for existence of train, validation, and test paths
        keys_with_required_vals = {'train', 'validation', 'test'}
        keys_with_vals = {k for k, v in self.config["data_file_patterns"].items() if v}
        keys_missing_required_vals = keys_with_required_vals.difference(keys_with_vals)
        if keys_missing_required_vals:
            msg = ("missing key(s) or value(s) within data_file_patterns for "
                   ", ".join(keys_missing_required_vals))
            raise ConfigError(msg)
        
        # check for score_dir value if we want to save model scores
        if self.config["save_scores"]:
            if not self.config.get("score_dir", None):
                raise ConfigError(f"missing score_dir key or value")
        
        # check that features is a list of length >= 1
        if type(self.config["features"]) != list or len(self.config["features"]) < 1:
            raise ConfigError("features must be a list with len >= 1")
        
        # specify valid supervised and unsupervised models
        supervised_models = {
          'XGBClassifier', 'XGBRegressor',
          'RandomForestClassifier', 'RandomForestRegressor',
          'DecisionTreeClassifier', 'DecisionTreeRegressor',
          'MLPClassifier', 'MLPRegressor',
          'KNeighborsClassifier', 'KNeighborsRegressor', 
          'LogisticRegression', 'LinearRegression'
        }
        unsupervised_models = {'KMeans', 'DBSCAN', 'IsolationForest'}
        valid_model_types = supervised_models | unsupervised_models | {'Other'}
        
        # check that model_type is valid
        if self.config["model_type"] not in valid_model_types:
            raise ConfigError(f"invalid model_type: {model_type}")
        
        # check supervised is consistent with model_type
        if self.config["model_type"] in supervised_models and not self.config["supervised"]:
            raise ConfigError(f"supervised should be True when model_type = {model_type}")
        if self.config["model_type"] in unsupervised_models and self.config["supervised"]:
            raise ConfigError(f"supervised should be False when model_type = {model_type}")
        
        # check label is consistent with supervised
        if self.config["supervised"] and not self.config.get("label", None):
            raise ConfigError(f"need label when supervised = True")
        
        # check eval_metric
        if "eval_metric" in self.config["hyperparameters"]:            
            if not isinstance(self.config["hyperparameters"]["eval_metric"], list):
                if not isinstance(self.config["hyperparameters"]["eval_metric"], str):
                    raise ConfigError(f"eval_metric should be a string or list")
                eval_metric = [self.config["hyperparameters"]["eval_metric"]]
            else:
                eval_metric = self.config["hyperparameters"]["eval_metric"]
            valid_metrics = {
                'rmse', 'rmsle', 'mae', 'mape', 'mphe', 'logloss', 'error', 'merror',
                'mlogloss', 'poisson-nloglik', 'gamma-nloglik', 'cox-nloglik',
                'gamma-deviance', 'tweedie-nloglik', 'aft-nloglik', 'auc', 'aucpr'
            }
            for m in eval_metric:
                if m not in valid_metrics:
                    raise ConfigError(f"{m} is not a valid eval_metric")
        
        # check that hyperparamter tuning algorithm is valid
        if self.config["hyperparameter_tuning"]:
            if self.config.get("tuning_algorithm", None) not in ("grid", "random", "tpe", "atpe"):
                msg = f'tuning_algorithm value must be in ("grid", "random", "tpe", "atpe")'
                raise ConfigError(msg)
            if self.config["tuning_algorithm"] in ("random", "tpe", "atpe"):
                if not self.config.get("tuning_iterations", None):
                    msg = "must specify tuning_iterations for the chosen tuning_algorithm"
                    raise ConfigError(msg)
            if "hyperparameter_eval_metric" not in self.config:
                msg = "must include hyperparameter_eval_metric when hyperparameter_tuning is True"
                raise ConfigError(msg)
            valid_metrics = {'average_precision', 'aucpr', 'auc', 'log_loss', 'brier_loss'}
            if self.config["hyperparameter_eval_metric"] not in valid_metrics:
                raise ConfigError(f"invalid hyperparameter_eval_metric value")

        # check that tuning_parameters is valid
        if self.config["hyperparameter_tuning"]:
            # check tuning_parameters has a value
            if not self.config["tuning_parameters"]:
                msg = "when hyperparameter_tuning is True, tuning_parameters must be specified"
                raise ConfigError(msg)
            # check tuning_parameters is a dictionary
            if not isinstance(self.config["tuning_parameters"], dict):
                msg = "when hyperparameter_tuning is True, tuning_parameters must be a dictionary"
                raise ConfigError(msg)
            # for grid search, check that tuning_parameters specifies lists of possible values
            if self.config["tuning_algorithm"] == "grid":
                for k, v in self.config["tuning_parameters"].items():
                    if not isinstance(v, list):
                        raise ConfigError(f"The tuning_parameters value of {k} must be a list")
            # for hyperopt search, check that tuning_parameters specifies valid values
            if self.config["tuning_algorithm"] in ("random", "tpe", "atpe"):
                for hyperparameter, distribution in self.config["tuning_parameters"].items():
                    # check that both the function and params are specified
                    if set(distribution.keys()) != {'function', 'params'}:
                        msg = (f"tuning_parameters.{hyperparameter} must contain"
                                " 'function' and 'params' keys, and no others")
                        raise ConfigError(msg)
                    func = distribution['function']
                    # check that all and only all valid params are included
                    func_to_params = {
                        "choice": {'options'},
                        "uniform": {'low', 'high'},
                        "quniform": {'low', 'high', 'q'},
                        "normal": {'mu', 'sigma'}
                    }
                    if set(distribution['params'].keys()) != func_to_params[func]:
                        msg = (f"tuning_parameters.{hyperparameter}.params must contain"
                               f" the following keys and no others: {func_to_params[func]}.")
                        raise ConfigError(msg)
                    # check that all params have valid values
                    param_to_datatype = {
                        "options": list,
                        "low": (int, float),
                        "high": (int, float),
                        "q": (int, float),
                        "mu": (int, float),
                        "sigma": (int, float)
                    }
                    for param, value in distribution['params'].items():
                        valid_type = param_to_datatype[param]
                        if not isinstance(value, valid_type):
                            msg = (f"tuning_parameters.{hyperparameter}.params.{param} must"
                                   f" be of type: {valid_type}")
                            raise ConfigError(msg)
                        if param == "sigma" and value <= 0:
                            msg = (f"tuning_parameters.{hyperparameter}.params.{param} must"
                                    " be > 0")
                            raise ConfigError(msg)

        # Note: Not checking perm_imp_metrics since there are many possible values that work
        
        # check perm_imp_n_repeats
        try:
            int(self.config.get("perm_imp_n_repeats", 10))
        except Exception as e:
            raise ConfigError(f"perm_imp_n_repeats exception converting to int: {e}")
        
        # check shap_sample (either no key, None or castable to int)
        shap_sample = self.config.get("shap_sample", None)
        if shap_sample is not None:
            try:
                int(shap_sample)
            except Exception as e:
                raise ConfigError(f"shap_sample exception converting to int: {e}")
        
        # check required boolean keys
        boolean_keys = {
            'save_scores', 'supervised', 'binary_classification','hyperparameter_tuning'
        }
        for k in boolean_keys:
            if self.config[k] not in (True, False, None):
                raise ConfigError(f"{k} must be True, False, or empty")
        
        # check non-required boolean keys
        boolean_keys = {'permutation_importance', 'shap'}
        for k in boolean_keys:
            if self.config.get(k, None) not in (True, False, None):
                raise ConfigError(f"if {k} is present, it must be True, False, or empty")

    def run(self):
        """Run a complete experiment including (depending on config):
            1) data loading
            2) hyperparameter tuning (grid search, random search, tpe, or atpe)
            3) model training
            4) saving the model object
            5) extensive model evaluation
            6) generating model explanitory artifacts
            7) saving model scores
        """

        self.setup()
        self.train()
        self.save_model()
        self.evaluate()
        self.explain()
        self.gen_scores()

    def setup(self):
        """
        Setup experiment by loading data, making directories for
        experiment output, and saving the config file.
        """

        # make output dirs
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.performance_dir.mkdir(exist_ok=True)
        
        # copy config to output_dir
        copyfile(self.config_path, self.output_dir/"config.yaml")
        print(f"config copied to {self.output_dir}/config.yaml")
        
        # load model
        if not self.model:
            self.load_model()
        
        # load data
        self.load_data()

    def load_model(self, model_obj=None, path=None):
        """
        Loads a model object from a parameter or file path, or instantiates a 
        new model object.

        Parameters
        ----------
            model_obj : str, optional
                scikit-learn model object with a .predict_proba() method
                (default is None)
            path : str, optional
                file path to scikit-learn model object with a .predict_proba()
                method (default is None)
        """

        # use generic scikit-learn model object (if passed in)
        if model_obj is not None:
            if self.model_type != 'Other':
                msg = (f"model_type should be 'Other', not {self.model_type} if a"
                        " model_obj is being passed into .load_model()")
                raise ConfigError(msg)
            if not isinstance(model_obj, base.BaseEstimator):
                msg = f"model_obj should be a scikit-learn model object, not {type(model_obj)}"
                raise TypeError(msg)
            self.model = model_obj
            print("model loaded from passed in model object")
        
        # load model from path (if passed in)
        elif path is not None:
            self._load_model_from_path(path)
        
        # load model from path (if specified in config)
        elif self.input_model_path:
            self._load_model_from_path(self.input_model_path)
        
        # instantiate new model
        else:
            print("Initializing model")
            self.model = {
                'XGBClassifier': xgb.XGBClassifier(),
                'XGBRegressor': xgb.XGBRegressor(),
                'RandomForestClassifier': ensemble.RandomForestClassifier(),
                'RandomForestRegressor': ensemble.RandomForestRegressor(),
                'DecisionTreeClassifier': tree.DecisionTreeClassifier(),
                'DecisionTreeRegressor': tree.DecisionTreeRegressor(),
                'MLPClassifier': neural_network.MLPClassifier(),
                'MLPRegressor': neural_network.MLPRegressor(),
                'KNeighborsClassifier': neighbors.KNeighborsClassifier(),
                'KNeighborsRegressor': neighbors.KNeighborsRegressor(),
                'LogisticRegression': linear_model.LogisticRegression(),
                'LinearRegression': linear_model.LinearRegression(),
                'KMeans': cluster.KMeans(),
                'DBSCAN': cluster.DBSCAN(),
                'IsolationForest': ensemble.IsolationForest()
            }[self.model_type]

    def _load_model_from_path(self, path):
        """
        Loads a model object from a file path.

        Parameters
        ----------
            path : str
                file path to scikit-learn model object with a .predict_proba() method
        """

        print(f"Loading model object from {path}")
        with open(path, 'rb') as f:
            self.model = pickle.load(f, encoding='latin1')
            print(f"model loaded from path: {path}")

    def load_data(self):
        """
        Loads in data, including training, validation, and testing data,
        and possibly other datasets as specified in the config.
        """

        print(f"\n-----Loading Data-----")
        
        # get fields to load (features + label + auxiliary fields)
        fields = self.features[:]
        if self.supervised:
            fields.append(self.label)
        for f in self.aux_fields:
            if f is not None and f not in fields:
                fields.append(f)
        
        # load data
        for name, file_pattern in self.data_file_patterns.items():
            data_path = self.data_dir / file_pattern
            print(f"loading {name} data from {data_path}")
            df = dd.read_csv(data_path, usecols=fields)
            df = df.compute()
            df = shuffle(df, random_state=self.seed)
            self.data[name] = {
                'X': df[self.features]
            }
            # include label in supervised learning experiments
            if self.supervised:
                self.data[name]['y'] = df[self.label]
            # store aux data (in separate object so that **self.data[name] can be used) 
            self.aux_data[name] = df[self.aux_fields]

    def train(self):
        """
        tune hyperparameters, then train a final model with the tuned
        hyperparmeters.
        """

        # initialize and tune hyperparamters
        self.tune_hyperparameters()
        
        # train model with optimal paramaters
        print(f"\n-----Training Final Model-----")
        self.model.fit(**self.data['train'])

    def tune_hyperparameters(self):
        """
        Tune hyperparameters with either grid search, random search, tpe,
        or atpe.
        """

        # initialize hyperparamters
        self.model.set_params(**self.hyperparameters)
        
        # only tune if configured to
        if not (self.supervised and self.hyperparameter_tuning):
            return  

        print(f"\n-----Tuning Hyperparameters (via {self.tuning_algorithm} search)-----")
        
        # run grid search (if configured)
        if self.tuning_algorithm == 'grid':
            best_params = self.grid_search()
        
        # run random search, tpe, or atpe hyperparameter optimization algorithm 
        elif self.tuning_algorithm in ("random", "tpe", "atpe"):
            best_params = self.hyperopt_search()
        
        # write best params to file
        with open(self.performance_dir/"parameter_tuning_log.txt", "a") as file:
            file.write(f"Best parameters: {best_params}\n\n")
        
        # set model to use best paramaters 
        self.model.set_params(**best_params)

    def grid_search(self):
        """Tune hyperparameters with grid search."""

        # Grid search all possible combinations
        param_dict_list = ParameterGrid(self.tuning_parameters)
        scores = []
        for i, param_dict in enumerate(param_dict_list):
            print(f"{i+1} out of {len(param_dict_list)}")
            score = self._train_eval_iteration(param_dict)
            scores.append(score)
        
        # get parameter set with best score
        if self.hyperparameter_eval_metric in {'average_precision', 'aucpr', 'auc'}:
            best = np.argmax
        elif self.hyperparameter_eval_metric in {'log_loss', 'brier_loss'}:
            best = np.argmin
        best_params = param_dict_list[best(scores)]
        return best_params

    def hyperopt_search(self):
        """Tune hyperparameters with either random search, tpe, or atpe."""
        
        # define optimization function
        def objective(param_dict):
            score = self._train_eval_iteration(param_dict)
            # if metric is to be maximized, then negate score, since objective() gets minimized
            if self.hyperparameter_eval_metric in {'average_precision', 'aucpr', 'auc'}:
                score = -score
            return {'loss': score, 'status': STATUS_OK}

        # map param kwargs to positional args since atpe only works with positional arguments
        def kwargs_to_args(distribution):
            dist_func_str = distribution['function']
            params = distribution['params']
            if dist_func_str == "choice":
                return (params["options"], )
            elif dist_func_str == "uniform":
                return (params["low"], params["high"])
            elif dist_func_str == "quniform":
                return (params["low"], params["high"], params["q"])
            elif dist_func_str == "normal":
                return (params["mu"], params["sigma"])

        # get parameter space
        space = {}
        distribution_functions = {
            'choice': hp.choice,
            'uniform': hp.uniform,
            # change quniform space to integers
            'quniform': lambda hyperparam, *params: pyll.scope.int(hp.quniform(hyperparam, *params)),
            'normal': hp.normal,
        }
        
        for hyperparam, distribution in self.tuning_parameters.items():
            # get the distribution function and parameters which specify the distribution
            # of possible hyperparameter values
            dist_func = distribution_functions[distribution['function']]
            params = kwargs_to_args(distribution) # params = distribution['params']
            # add hyperparameter distribution to the hyperparameter space
            space[hyperparam] = dist_func(hyperparam, *params)

        # get tuning algorithm
        algo = {
            "random": rand.suggest,
            "tpe": tpe.suggest,
            "atpe": atpe.suggest,
        }[self.tuning_algorithm]

        # Run hyperparameter tuning
        trials = Trials()
        best_params = fmin(fn=objective, space=space, algo=algo, 
            max_evals=self.tuning_iterations, trials=trials, 
            rstate=np.random.default_rng(self.seed))

        # correct for min casting integers to floats by casting them back to ints
        for hyperparameter, value in best_params.items():
            if int(value) == value:
                best_params[hyperparameter] = int(value)

        # save trial output
        with open(self.performance_dir/"parameter_tuning_trials.txt", "a") as file:
            for trial in trials.trials:
                file.write(str(trial))
                file.write("\n\n")

        return best_params

    def _train_eval_iteration(self, param_dict):
        """
        Run one iteration of training and evaluating a model for
        hyperparameter tuning.

        Parameters
        ----------
            param_dict : dict
                dict of parameters to configure an scikit-learn model object
        """

        start_time = time.time()
        print(param_dict)
        
        # train model
        self.model.set_params(**param_dict)
        self.model.fit(**self.data['train'])
        
        # evaluate model (based on self.hyperparameter_eval_metric)
        y_true = self.data['validation']['y']
        y_score = self.model.predict_proba(self.data['validation']['X'])
        metric = self.hyperparameter_eval_metric
        score = metric_score(y_true, y_score, metric)
        
        # print and log results
        print(f"{metric}: {score}")
        seconds_to_train = time.time() - start_time
        print(f"{seconds_to_train} seconds to train")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.performance_dir/"parameter_tuning_log.txt", "a") as file:
            msg = (f"{now}\nParameters: {param_dict}\n{metric}: {score}\n{seconds_to_train}"
                    " seconds to train\n\n")
            file.write(msg)
        return score

    def save_model(self):
        """Save model object to file."""

        self.model_dir.mkdir(exist_ok=True)
        with open(self.model_dir/'model.pkl', 'wb') as file:
            pickle.dump(self.model, file)
        # TODO (?): also save pmml

    def evaluate(self, increment=0.01):
        """Evaluate model and generate performance charts."""
        
        if not self.supervised:
            return

        datasets = [(self.data[n]['X'], self.data[n]['y'], n) for n in self.dataset_names] 
        self.model_eval = ModelEvaluation(self.model, datasets, self.performance_dir, self.aux_fields)

        # generate binary classification metrics
        if self.binary_classification:
            self.model_eval.binary_evaluate(increment)

    def explain(self):
        """
        Generate model explanitory charts including feature importance
        and shap values.
        """

        # Generate permutation feature importance tables
        if self.permutation_importance:
            print(f"\n-----Generating Permutation Feature Importances-----")
            self.explain_dir.mkdir(exist_ok=True)
            metrics = self.perm_imp_metrics
            for name in self.dataset_names:
                r = permutation_importance(self.model, **self.data[name],
                    n_repeats=self.perm_imp_n_repeats, random_state=self.seed,
                    scoring=metrics)
                imps = []
                for m in metrics:
                    means = pd.Series(r[m]['importances_mean'], name=f"{m}_mean")
                    stds = pd.Series(r[m]['importances_std'], name=f"{m}_std")
                    imps.extend([means, stds])
                df = pd.concat(imps, axis=1) # dataframe of importance means and stds
                df.index = self.features
                df.sort_values(f"{metrics[0]}_mean", ascending=False, inplace=True)
                df.to_csv(f'{self.explain_dir}/permutation_importance_{name}.csv')

        # Generate Shap Charts
        if self.shap:
            self._plot_shap()

    def _plot_shap(self):
        """Generate model explanitory charts involving shap values."""

        assert self.shap, "self.shap must be True to run ._plot_shap()"
        plt.close('all')
        
        # Generate Shap Charts
        print(f"\n-----Generating Shap Charts-----")
        savefig_kwargs = {'bbox_inches': 'tight', 'pad_inches': 0.2}
        predict = lambda x: self.model.predict_proba(x)[:,1]
        for dataset_name in self.dataset_names:
            
            # get sample of dataset (gets all data if self.shap_sample is None)
            dataset = self.data[dataset_name]['X'].iloc[:self.shap_sample]
            if len(dataset) > 500000:
                msg = (f"Shap will be slow on {len(dataset)} rows, consider using"
                        " shap_sample in the config to sample fewer rows")
                warnings.warn(msg)

            # Generate partial dependence plots 
            print(f'Plotting {dataset_name} partial dependence plots')
            plot_dir = self.explain_dir/"shap"/dataset_name/"partial_dependence_plots"
            plot_dir.mkdir(parents=True, exist_ok=True)
            for feature in tqdm(self.features):        
                fig, ax = shap.partial_dependence_plot(
                    feature, predict, dataset, model_expected_value=True,
                    feature_expected_value=True, show=False, ice=False)
                fig.savefig(f"{plot_dir}/{feature}.png", **savefig_kwargs)
                plt.close()

            # Generate scatter plots (coloured by feature with strongest interaction)
            print(f'Plotting {dataset_name} scatter plots')
            explainer = shap.Explainer(self.model, dataset)
            shap_values = explainer(dataset)
            plot_dir = self.explain_dir/"shap"/dataset_name/"scatter_plots"
            plot_dir.mkdir(exist_ok=True)
            for feature in tqdm(self.features):        
                shap.plots.scatter(shap_values[:,feature], alpha=0.3, 
                    color=shap_values, show=False)
                plt.savefig(f"{plot_dir}/{feature}.png", **savefig_kwargs)
                plt.close()

            # Generate beeswarm plot
            print(f'Plotting {dataset_name} beeswarm plot')
            shap.plots.beeswarm(shap_values, alpha=0.1, max_display=1000, show=False)
            path = self.explain_dir/"shap"/dataset_name/"beeswarm_plot.png"
            plt.savefig(path, **savefig_kwargs)
            plt.close()

            # Generate bar plots
            print(f'Plotting {dataset_name} bar plots')
            shap.plots.bar(shap_values, max_display=1000, show=False)
            path = self.explain_dir/"shap"/dataset_name/"abs_mean_bar_plot.png"
            plt.savefig(path, **savefig_kwargs)
            plt.close()
            shap.plots.bar(shap_values.abs.max(0), max_display=1000, show=False)
            path = self.explain_dir/"shap"/dataset_name/"abs_max_bar_plot.png"
            plt.savefig(path, **savefig_kwargs)
            plt.close()
            # TODO?: make alpha and max_display config variables

    def gen_scores(self):
        """Save model scores for each row"""

        if not self.save_scores:
            return
        print(f"\n-----Generating Scores-----")
        self.score_dir.mkdir(exist_ok=True)
        for k, v in self.data.items():
            scores = self.model.predict_proba(v['X'])[:,1]
            scores = pd.Series(scores, name='score', index=v['y'].index)
            df = pd.concat([v['y'], scores, self.aux_data[k]], axis=1)
            path = f"{self.score_dir}/{k}_scores"
            df.to_csv(path, index=False)
            print(f"Saved {k} scores to {path}")

      