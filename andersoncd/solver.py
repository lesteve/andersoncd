import numpy as np
from numba import njit
from scipy import sparse
from sklearn.utils import check_array


def solver_path(X, y, datafit, penalty, eps=1e-3, n_alphas=100, alphas=None,
                coef_init=None, max_iter=20, max_epochs=50_000,
                p0=10, tol=1e-4, prune=0,
                return_n_iter=False, verbose=0,):
    r"""Compute optimization path with Celer primal as inner solver.

    The loss is customized by passing various choices of datafit and penalty:
    loss = datafit.value() + penalty.value()


    Parameters
    ----------
    X : ndarray, shape (n_samples, n_features)
        Training data.

    y : ndarray, shape (n_samples,)
        Target values.

    datafit: instance of Datafit class
        Datafitting term.

    penalty : instance of Penalty class
        Penalty used in the model.

    eps : float, optional
        Length of the path. ``eps=1e-3`` means that
        ``alpha_min = 1e-3 * alpha_max``

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : ndarray, optional
        List of alphas where to compute the models.
        If ``None`` alphas are set automatically

    coef_init : ndarray, shape (n_features,) | None, optional, (default=None)
        Initial value of coefficients. If None, np.zeros(n_features) is used.

    max_iter : int, optional
        The maximum number of iterations (definition of working set and
        resolution of problem restricted to features in working set)

    max_epochs : int, optional
        Maximum number of (block) CD epochs on each subproblem.

    p0 : int, optional
        First working set size.

    verbose : bool or integer, optional
        Amount of verbosity. 0/False is silent

    tol : float, optional
        The tolerance for the optimization.

    prune : bool, optional
        Whether or not to use pruning when growing working sets.

    X_offset : np.array, shape (n_features,), optional
        Used to center sparse X without breaking sparsity. Mean of each column.
        See sklearn.linear_model.base._preprocess_data().

    X_scale : np.array, shape (n_features,), optional
        Used to scale centered sparse X without breaking sparsity. Norm of each
        centered column. See sklearn.linear_model.base._preprocess_data().

    return_n_iter : bool, optional
        If True, number of iterations along the path are returned.


    Returns
    -------
    alphas : array, shape (n_alphas,)
        The alphas along the path where models are computed.

    coefs : array, shape (n_features, n_alphas)
        Coefficients along the path.

    kkt_max : array, shape (n_alphas,)
        Maximum violation of KKT along the path.
    """

    X = check_array(X, 'csc', dtype=[np.float64, np.float32],
                    order='F', copy=False, accept_large_sparse=False)
    y = check_array(y, 'csc', dtype=X.dtype.type, order='F', copy=False,
                    ensure_2d=False)

    if sparse.issparse(X):
        datafit.initialize_sparse(X.data, X.indptr, X.indices, y)
    else:
        datafit.initialize(X, y)
    n_features = X.shape[1]

    # if X_offset is not None:
    #     X_sparse_scaling = X_offset / X_scale
    #     X_sparse_scaling = np.asarray(X_sparse_scaling, dtype=X.dtype)
    # else:
    #     X_sparse_scaling = np.zeros(n_features, dtype=X.dtype)

    # X_dense, X_data, X_indices, X_indptr = _sparse_and_dense(X)

    if alphas is None:
        # TODO pass datafit.gradient at 0
        alpha_max = penalty.alpha_max(X, y)
        alphas = alpha_max * np.geomspace(1, eps, n_alphas, dtype=X.dtype)
    else:
        alphas = np.sort(alphas)[::-1]

    n_alphas = len(alphas)

    coefs = np.zeros((n_features, n_alphas), order='F', dtype=X.dtype)
    kkt_maxs = np.zeros(n_alphas)

    if return_n_iter:
        n_iters = np.zeros(n_alphas, dtype=int)

    for t in range(n_alphas):

        alpha = alphas[t]
        penalty.alpha = alpha  # TODO this feels it will break sklearn compat
        if verbose:
            to_print = "##### Computing alpha %d/%d" % (t + 1, n_alphas)
            print("#" * len(to_print))
            print(to_print)
            print("#" * len(to_print))
        if t > 0:
            w = coefs[:, t - 1].copy()
            p0 = max(len(np.where(w != 0)[0]), 1)
        else:
            if coef_init is not None:
                w = coef_init.copy()
                p0 = max((w != 0.).sum(), p0)
                Xw = X @ w
            else:
                w = np.zeros(n_features, dtype=X.dtype)
                Xw = np.zeros_like(y)

        sol = solver(
            X, y, datafit, penalty, w, Xw,
            max_iter=max_iter, max_epochs=max_epochs, p0=p0, tol=tol,
            verbose=verbose)

        coefs[:, t] = w.copy()
        kkt_maxs[t] = sol[-1]

        if return_n_iter:
            n_iters[t] = len(sol[1])

    results = alphas, coefs, kkt_maxs
    if return_n_iter:
        results += (n_iters,)

    return results


def solver(
        X, y, datafit, penalty, w, Xw, max_iter=50,
        max_epochs=50_000, p0=10, tol=1e-4, use_acc=True, K=5, verbose=0):
    """
    datafit : instance of Datafit
    penalty: instance of Penalty
    p0: first size of working set.
    """
    n_features = X.shape[1]
    pen = penalty.is_penalized(n_features)
    unpen = ~pen
    n_unpen = unpen.sum()
    obj_out = []
    all_feats = np.arange(n_features)

    is_sparse = sparse.issparse(X)
    for t in range(max_iter):

        if is_sparse:
            kkt = _kkt_violation_sparse(
                w, X.data, X.indptr, X.indices, y, Xw, datafit, penalty,
                all_feats)
        else:
            kkt = _kkt_violation(
                w, X, y, Xw, datafit, penalty, all_feats)
        kkt_max = np.max(kkt)
        if verbose:
            print(f"KKT max violation: {kkt_max:.2e}")
        if kkt_max <= tol:
            break
        # 1) select features : all unpenalized, + 2 * (nnz and penalized)
        ws_size = max(p0 + n_unpen,
                      min(2 * (w != 0).sum() - n_unpen, n_features))

        kkt[unpen] = np.inf  # always include unpenalized features
        kkt[w != 0] = np.inf  # TODO check
        ws = np.argsort(kkt)[-ws_size:]

        if use_acc:
            last_K_w = np.zeros([K + 1, n_features])
            U = np.zeros([K, n_features])

        if verbose:
            print(f'Iteration {t + 1}, {ws_size} feats in subpb.')

        # 2) do iterations on smaller problem
        is_sparse = sparse.issparse(X)
        for epoch in range(max_epochs):
            if is_sparse:
                _cd_epoch_sparse(
                    X.data, X.indptr, X.indices, y, w, Xw, datafit, penalty,
                    ws)
            else:
                _cd_epoch(X, y, w, Xw, datafit, penalty, ws)

            # TODO optimize computation using ws
            if use_acc:
                last_K_w[epoch % (K + 1)] = w

                if epoch % (K + 1) == K:
                    for k in range(K):
                        U[k] = last_K_w[k + 1] - last_K_w[k]
                    C = np.dot(U, U.T)

                    try:
                        z = np.linalg.solve(C, np.ones(K))
                        c = z / z.sum()
                        w_acc = w.copy()
                        w_acc = np.sum(
                            last_K_w[:-1] * c[:, None], axis=0)
                        p_obj = datafit.value(y, w, Xw) + penalty.value(w)
                        Xw_acc = X @ w_acc
                        p_obj_acc = datafit.value(
                            y, w_acc, Xw_acc) + penalty.value(w_acc)
                        if p_obj_acc < p_obj:
                            w[:] = w_acc
                            Xw[:] = Xw_acc
                    except np.linalg.LinAlgError:
                        if max(verbose - 1, 0):
                            print("----------Linalg error")

            if epoch % 10 == 0:
                # todo maybe we can improve here by restricting to ws
                p_obj = datafit.value(y, w, Xw) + penalty.value(w)

                if is_sparse:
                    kkt_ws = _kkt_violation_sparse(
                        w, X.data, X.indptr, X.indices, y, Xw, datafit,
                        penalty, ws)
                else:
                    kkt_ws = _kkt_violation(
                        w, X, y, Xw, datafit, penalty, ws)

                kkt_ws_max = np.max(kkt_ws)
                if max(verbose - 1, 0):
                    print(f"    Epoch {epoch}, objective {p_obj:.10f}, "
                          f"kkt {kkt_ws_max:.2e}")
                if kkt_ws_max < 0.3 * kkt_max:
                    if max(verbose - 1, 0):
                        print("    Early exit")
                    break
        obj_out.append(p_obj)
    return w, np.array(obj_out), kkt_max


@njit
def _kkt_violation(w, X, y, Xw, datafit, penalty, ws):
    grad = np.zeros(ws.shape[0])
    for idx, j in enumerate(ws):
        grad[idx] = datafit.gradient_scalar(X, y, w, Xw, j)
    return penalty.subdiff_distance(w, grad, ws)


@njit
def _kkt_violation_sparse(
        w, data, indptr, indices, y, Xw, datafit, penalty, ws):
    grad = np.zeros(ws.shape[0])
    for idx, j in enumerate(ws):
        Xj = data[indptr[j]:indptr[j+1]]
        idx_nz = indices[indptr[j]:indptr[j+1]]
        grad[idx] = datafit.gradient_scalar_sparse(Xj, idx_nz, y, Xw, j)
    return penalty.subdiff_distance(w, grad, ws)


@njit
def _cd_epoch(X, y, w, Xw, datafit, penalty, feats):
    lc = datafit.lipschitz
    for j in feats:
        Xj = X[:, j]
        old_w_j = w[j]
        w[j] = penalty.prox_1d(
            old_w_j - datafit.gradient_scalar(X, y, w, Xw, j) / lc[j],
            1 / lc[j], j)
        if w[j] != old_w_j:
            Xw += (w[j] - old_w_j) * Xj


@njit
def _cd_epoch_sparse(
        data, indptr, indices, y, w, Xw, datafit, penalty, feats):
    lc = datafit.lipschitz
    for j in feats:
        Xj = data[indptr[j]:indptr[j+1]]
        idx_nz = indices[indptr[j]:indptr[j+1]]

        old_w_j = w[j]
        gradj = datafit.gradient_scalar_sparse(
            Xj, idx_nz, y, Xw, j)
        w[j] = penalty.prox_1d(
            old_w_j - gradj / lc[j], 1 / lc[j], j)
        if w[j] != old_w_j:
            Xw[idx_nz] += (w[j] - old_w_j) * Xj
