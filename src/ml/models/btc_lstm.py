"""
LSTM/GRU neural network for BTC price direction prediction.

Sprint 2 Task 2.2 -- predicts BTC price direction over next 15min/1hr
using sequences of FeaturePipeline-computed features.
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

logger = logging.getLogger("MoneyPrinter")

# Default save location (relative to project root)
_DEFAULT_MODEL_DIR = Path("data/models")
_DEFAULT_MODEL_NAME = "btc_lstm_latest.pt"


class _LSTMNet(nn.Module):
    """Internal PyTorch LSTM module.

    Architecture:
        LSTM (multi-layer, dropout) -> final hidden state -> Linear -> Sigmoid
    Input shape:  (batch, seq_len, input_size)
    Output shape: (batch, 1)
    """

    def __init__(
        self,
        input_size: int,
        hidden_size: int,
        num_layers: int,
        dropout: float,
    ) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout if num_layers > 1 else 0.0,
            batch_first=True,
        )
        self.dropout = nn.Dropout(p=dropout)
        self.fc = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, seq_len, input_size)
        lstm_out, _ = self.lstm(x)
        # Use the last timestep's output
        last_hidden = lstm_out[:, -1, :]  # (batch, hidden_size)
        out = self.dropout(last_hidden)
        out = self.fc(out)  # (batch, 1)
        out = self.sigmoid(out)
        return out


class BTCLSTMPredictor:
    """BTC price direction predictor using an LSTM neural network.

    Wraps :class:`_LSTMNet` with training, prediction, sequence
    preparation, built-in feature normalisation, and model persistence.

    Parameters
    ----------
    input_size : int
        Number of features per timestep (default 26, matching
        :class:`FeaturePipeline`).
    hidden_size : int
        LSTM hidden dimension.
    num_layers : int
        Number of stacked LSTM layers.
    dropout : float
        Dropout rate applied between LSTM layers and before the FC head.
    sequence_length : int
        Number of past timesteps per sample (default 60 = 60 one-minute
        candles).
    model_path : str, optional
        If provided **and** the file exists, the saved model is loaded
        immediately.
    """

    def __init__(
        self,
        input_size: int = 26,
        hidden_size: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
        sequence_length: int = 60,
        model_path: str = None,
    ) -> None:
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.sequence_length = sequence_length

        self.device = torch.device("cpu")

        # Built-in StandardScaler parameters (fit during training)
        self._scaler_mean: Optional[np.ndarray] = None  # shape (input_size,)
        self._scaler_std: Optional[np.ndarray] = None  # shape (input_size,)
        self._scaler_fitted = False

        # Build network
        self.model = self._build_model()

        # If a saved model exists, load it
        if model_path is not None and os.path.isfile(model_path):
            self.load(model_path)

    # ------------------------------------------------------------------
    # Model construction
    # ------------------------------------------------------------------

    def _build_model(self) -> _LSTMNet:
        net = _LSTMNet(
            input_size=self.input_size,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            dropout=self.dropout,
        )
        net.to(self.device)
        return net

    # ------------------------------------------------------------------
    # Built-in feature normalisation
    # ------------------------------------------------------------------

    def _fit_scaler(self, X: np.ndarray) -> None:
        """Compute per-feature mean/std from training data.

        Parameters
        ----------
        X : np.ndarray
            Shape (n_samples, seq_len, input_size).
        """
        # Flatten samples and timesteps to compute feature-level stats
        flat = X.reshape(-1, X.shape[-1])  # (n_samples * seq_len, input_size)
        self._scaler_mean = flat.mean(axis=0).astype(np.float32)
        self._scaler_std = flat.std(axis=0).astype(np.float32)
        # Guard against zero-std features (constant columns)
        self._scaler_std[self._scaler_std < 1e-8] = 1.0
        self._scaler_fitted = True

    def _transform(self, X: np.ndarray) -> np.ndarray:
        """Apply z-score normalisation using stored scaler params.

        Parameters
        ----------
        X : np.ndarray
            Shape (n_samples, seq_len, input_size).

        Returns
        -------
        np.ndarray
            Normalised copy with the same shape.
        """
        if not self._scaler_fitted:
            raise RuntimeError(
                "Scaler has not been fitted. Call train_model() first or "
                "load a model that includes scaler parameters."
            )
        return (X - self._scaler_mean) / self._scaler_std

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train_model(
        self,
        X: np.ndarray,
        y: np.ndarray,
        val_X: np.ndarray = None,
        val_y: np.ndarray = None,
        epochs: int = 50,
        batch_size: int = 32,
        lr: float = 0.001,
    ) -> Dict[str, float]:
        """Train the LSTM on labelled sequences.

        Parameters
        ----------
        X : np.ndarray
            Training features, shape (n_samples, sequence_length, input_size).
        y : np.ndarray
            Binary labels, shape (n_samples,).
        val_X, val_y : np.ndarray, optional
            Validation set.  If provided, the model with the best validation
            loss is kept.
        epochs : int
            Maximum number of training epochs.
        batch_size : int
            Mini-batch size.
        lr : float
            Adam learning rate.

        Returns
        -------
        dict
            ``{'best_val_loss': float, 'final_train_loss': float,
            'epochs_run': int}``
        """
        # Fit scaler on training data and normalise
        self._fit_scaler(X)
        X = self._transform(X)

        has_val = val_X is not None and val_y is not None
        if has_val:
            val_X = self._transform(val_X)

        # Convert to tensors
        X_t = torch.tensor(X, dtype=torch.float32, device=self.device)
        y_t = torch.tensor(y, dtype=torch.float32, device=self.device).unsqueeze(1)

        train_ds = TensorDataset(X_t, y_t)
        train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)

        if has_val:
            val_X_t = torch.tensor(val_X, dtype=torch.float32, device=self.device)
            val_y_t = torch.tensor(
                val_y, dtype=torch.float32, device=self.device
            ).unsqueeze(1)

        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        criterion = nn.BCELoss()

        best_val_loss = float("inf")
        best_state = None
        final_train_loss = float("inf")

        self.model.train()

        for epoch in range(1, epochs + 1):
            epoch_loss = 0.0
            n_batches = 0

            for batch_X, batch_y in train_loader:
                optimizer.zero_grad()
                preds = self.model(batch_X)
                loss = criterion(preds, batch_y)
                loss.backward()
                optimizer.step()

                epoch_loss += loss.item()
                n_batches += 1

            avg_train_loss = epoch_loss / max(n_batches, 1)
            final_train_loss = avg_train_loss

            # Validation
            val_loss_str = "N/A"
            if has_val:
                self.model.eval()
                with torch.no_grad():
                    val_preds = self.model(val_X_t)
                    val_loss = criterion(val_preds, val_y_t).item()
                self.model.train()

                val_loss_str = f"{val_loss:.6f}"

                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    best_state = {
                        k: v.clone() for k, v in self.model.state_dict().items()
                    }

            # Log every 10 epochs (and first + last)
            if epoch == 1 or epoch % 10 == 0 or epoch == epochs:
                logger.info(
                    f"[BTCLSTMPredictor] Epoch {epoch:>4d}/{epochs}  "
                    f"train_loss={avg_train_loss:.6f}  val_loss={val_loss_str}"
                )

        # Restore best model if we tracked validation
        if best_state is not None:
            self.model.load_state_dict(best_state)
            logger.info(
                f"[BTCLSTMPredictor] Restored best val checkpoint "
                f"(val_loss={best_val_loss:.6f})"
            )

        self.model.eval()

        return {
            "best_val_loss": best_val_loss if has_val else float("nan"),
            "final_train_loss": final_train_loss,
            "epochs_run": epochs,
        }

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return probability of the positive class (price going up).

        Parameters
        ----------
        X : np.ndarray
            Shape (n_samples, sequence_length, input_size).

        Returns
        -------
        np.ndarray
            Probabilities, shape (n_samples,).
        """
        X = self._transform(X)
        X_t = torch.tensor(X, dtype=torch.float32, device=self.device)

        self.model.eval()
        with torch.no_grad():
            probs = self.model(X_t).squeeze(-1)  # (n_samples,)
        return probs.cpu().numpy()

    def predict(self, X: np.ndarray, threshold: float = 0.5) -> np.ndarray:
        """Return binary predictions.

        Parameters
        ----------
        X : np.ndarray
            Shape (n_samples, sequence_length, input_size).
        threshold : float
            Decision boundary (default 0.5).

        Returns
        -------
        np.ndarray
            Binary array of shape (n_samples,), dtype int.
        """
        probs = self.predict_proba(X)
        return (probs >= threshold).astype(int)

    # ------------------------------------------------------------------
    # Sequence preparation
    # ------------------------------------------------------------------

    def prepare_sequences(
        self,
        df: pd.DataFrame,
        feature_cols: List[str] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Convert a feature DataFrame into sliding-window sequences.

        Parameters
        ----------
        df : pd.DataFrame
            Must contain ``feat_*`` columns (from :class:`FeaturePipeline`).
            Optionally contains a ``timestamp`` column.
        feature_cols : list of str, optional
            Explicit list of columns to use.  If *None*, all columns
            matching ``feat_*`` are selected automatically.

        Returns
        -------
        tuple of (X, timestamps)
            X : np.ndarray, shape (n_samples, sequence_length, n_features)
            timestamps : np.ndarray of corresponding end-of-window
            timestamps (or integer indices if no ``timestamp`` column).
        """
        if feature_cols is None:
            feature_cols = sorted([c for c in df.columns if c.startswith("feat_")])
        if not feature_cols:
            raise ValueError("No feature columns found in DataFrame.")

        values = df[feature_cols].values.astype(np.float32)

        # Handle NaN values left from indicator warm-up: forward-fill then zero-fill
        mask = np.isnan(values)
        if mask.any():
            temp_df = pd.DataFrame(values, columns=feature_cols)
            temp_df = temp_df.ffill().fillna(0.0)
            values = temp_df.values.astype(np.float32)

        has_ts = "timestamp" in df.columns
        ts_values = df["timestamp"].values if has_ts else np.arange(len(df))

        sequences = []
        timestamps = []
        seq_len = self.sequence_length

        for i in range(seq_len, len(values)):
            sequences.append(values[i - seq_len : i])
            timestamps.append(ts_values[i])

        X = np.array(sequences, dtype=np.float32)
        timestamps = np.array(timestamps)

        return X, timestamps

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str = None) -> None:
        """Save model weights, architecture config, and scaler params.

        Parameters
        ----------
        path : str, optional
            File path.  Defaults to ``data/models/btc_lstm_latest.pt``.
        """
        if path is None:
            path = str(_DEFAULT_MODEL_DIR / _DEFAULT_MODEL_NAME)

        os.makedirs(os.path.dirname(path), exist_ok=True)

        checkpoint = {
            "model_state_dict": self.model.state_dict(),
            "config": {
                "input_size": self.input_size,
                "hidden_size": self.hidden_size,
                "num_layers": self.num_layers,
                "dropout": self.dropout,
                "sequence_length": self.sequence_length,
            },
            "scaler": {
                "mean": self._scaler_mean,
                "std": self._scaler_std,
                "fitted": self._scaler_fitted,
            },
        }
        torch.save(checkpoint, path)
        logger.info(f"[BTCLSTMPredictor] Model saved to {path}")

    def load(self, path: str) -> None:
        """Load model weights, architecture config, and scaler params.

        Parameters
        ----------
        path : str
            Path to a ``.pt`` checkpoint file.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist.
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Model file not found: {path}")

        checkpoint = torch.load(path, map_location=self.device, weights_only=False)

        # Restore config and rebuild network if architecture changed
        cfg = checkpoint.get("config", {})
        rebuild = False
        for attr in ("input_size", "hidden_size", "num_layers", "dropout"):
            saved_val = cfg.get(attr)
            if saved_val is not None and saved_val != getattr(self, attr):
                setattr(self, attr, saved_val)
                rebuild = True

        if "sequence_length" in cfg:
            self.sequence_length = cfg["sequence_length"]

        if rebuild:
            self.model = self._build_model()

        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()

        # Restore scaler
        scaler = checkpoint.get("scaler", {})
        self._scaler_mean = scaler.get("mean")
        self._scaler_std = scaler.get("std")
        self._scaler_fitted = scaler.get("fitted", False)

        logger.info(f"[BTCLSTMPredictor] Model loaded from {path}")
