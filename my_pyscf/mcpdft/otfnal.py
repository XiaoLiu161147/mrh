import numpy as np
import copy
import re
from pyscf.lib import logger
from pyscf.dft.gen_grid import Grids
from pyscf.dft.numint import _NumInt, NumInt
from mrh.util import params
from mrh.my_pyscf.mcpdft import pdft_veff, tfnal_derivs

class otfnal:
    r''' Needs:
        mol: object of class pyscf.gto.mole
        grids: object of class pyscf.dft.gen_grid.Grids
        eval_ot: function with calling signature shown below
        _numint: object of class pyscf.dft.NumInt
            member functions "hybrid_coeff", "nlc_coeff, "rsh_coeff", and "_xc_type" (at least)
            must be overloaded; see below
        verbose: integer
            for PySCF's logger system
        stdout: record
            for PySCF's logger system
        otxc: string
            name of on-top pair-density exchange-correlation functional
    '''

    def __init__ (self, mol, **kwargs):
        self.mol = mol
        self.verbose = mol.verbose
        self.stdout = mol.stdout    

    Pi_deriv = 0

    def _init_info (self):
        logger.info (self, 'Building %s functional', self.otxc)
        omega, alpha, hyb = self._numint.rsh_and_hybrid_coeff(self.otxc, spin=self.mol.spin)
        if hyb[0] > 0:
            logger.info (self, 'Hybrid functional with %s CASSCF exchange', hyb)

    @property
    def xctype (self):
        return self._numint._xc_type (self.otxc)

    @property
    def dens_deriv (self):
        return ['LDA', 'GGA', 'MGGA'].index (self.xctype)

    def eval_ot (self, rho, Pi, dderiv=0, **kwargs):
        r''' Evaluate the on-dop energy and its functional derivatives on a grid

        Args:
            rho : ndarray of shape (2,*,ngrids)
                containing spin-density [and derivatives]
            Pi : ndarray with shape (*,ngrids)
                containing on-top pair density [and derivatives]

        Kwargs:
            dderiv : integer
                Order of derivatives to return

        Returns: 
            eot : ndarray of shape (ngrids)
                integrand of the on-top exchange-correlation energy
            vot : (array_like (rho), array_like (Pi)) or None
                first functional derivative of Eot wrt (density, pair density)
                and their derivatives
            fot : ndarray of shape (*,ngrids) or None
                second functional derivative of Eot wrt density, pair density,
                and derivatives; first dimension is lower-triangular matrix elements
                corresponding to the basis (rho, Pi, |drho|^2, drho'.dPi, |dPi|)
                stopping at Pi (3 elements) for t-LDA and |drho|^2 (6 elements)
                for t-GGA.
        '''

        raise RuntimeError("on-top xc functional not defined")
        return 0

    def get_E_ot (self, rho, Pi, weights):
        r''' Being phased out as redundant with eval_ot
        Left in place for backwards-compatibility'''

        assert (rho.shape[1:] == Pi.shape[:]), "rho.shape={0}, Pi.shape={1}".format (rho.shape, Pi.shape)
        if rho.ndim == 2:
            rho = np.expand_dims (rho, 1)
            Pi = np.expand_dims (Pi, 0)
        
        return self.eval_ot (rho, Pi, dderiv=0, weights=weights)[0].dot (weights)

    get_veff_1body = pdft_veff.get_veff_1body
    get_veff_2body = pdft_veff.get_veff_2body
    get_veff_2body_kl = pdft_veff.get_veff_2body_kl

class transfnal (otfnal):
    r''' "translated functional" of Li Manni et al., JCTC 10, 3669 (2014).
    '''

    def __init__ (self, ks, **kwargs):
        otfnal.__init__(self, ks.mol, **kwargs)
        self.otxc = 't' + ks.xc
        self._numint = copy.copy (ks._numint)
        self.grids = copy.copy (ks.grids)
        self._numint.hybrid_coeff = t_hybrid_coeff.__get__(self._numint)
        self._numint.nlc_coeff = t_nlc_coeff.__get__(self._numint)
        self._numint.rsh_coeff = t_rsh_coeff.__get__(self._numint)
        self._numint.eval_xc = t_eval_xc.__get__(self._numint)
        self._numint._xc_type = t_xc_type.__get__(self._numint)
        self._init_info ()

    eval_ot = tfnal_derivs.eval_ot

    def get_ratio (self, Pi, rho_avg):
        r''' R = Pi / [rho/2]^2 = Pi / rho_avg^2
            An intermediate quantity when computing the translated spin densities

            Note this function returns 1 for values and 0 for derivatives for every point where the charge density is close to zero (i.e., convention: 0/0 = 1)
        '''
        assert (Pi.shape == rho_avg.shape)
        nderiv = Pi.shape[0]
        if nderiv > 4:
            raise NotImplementedError("derivatives above order 1")

        R = np.zeros_like (Pi)  
        R[0,:] = 1
        idx = rho_avg[0] >= (1e-15 / 2)
        # Chain rule!
        for ideriv in range (nderiv):
            R[ideriv,idx] = Pi[ideriv,idx] / rho_avg[0,idx] / rho_avg[0,idx]
        # Product rule!
        for ideriv in range (1,nderiv):
            R[ideriv,idx] -= 2 * rho_avg[ideriv,idx] * R[0,idx] / rho_avg[0,idx]
        return R

    def get_rho_translated (self, Pi, rho, _fn_deriv=0, **kwargs):
        r''' Compute the "translated" alpha and beta densities:

        rho_t^a = (rho/2) * (1 + zeta)
        rho_t^b = (rho/2) * (1 - zeta)
        rho'_t^a = (rho'/2) * (1 + zeta)
        rho'_t^b = (rho'/2) * (1 - zeta)

        See "get_zeta" for the meaning of "zeta" 
    
        Args:
            Pi : ndarray of shape (*, ngrids)
                containing on-top pair density [and derivatives]
            rho : ndarray of shape (2, *, ngrids)
                containing spin density [and derivatives]
    
        Kwargs:
            _fn_deriv : integer
                Order of functional derivatives of zeta to compute.
                In "translated" functionals, no functional derivatives
                of zeta are used. This kwarg is used for convenience
                when calling from children classes. It changes the return
                signature and should not normally be touched by users.
    
        Returns: 
            rho_t : ndarray of shape (2,*,ngrids)
                Translated spin density (and derivatives)
        '''
    
        # For nonzero charge & pair density, set alpha dens = beta dens = 1/2 charge dens
        rho_avg = (rho[0,:,:] + rho[1,:,:]) / 2
        rho_t = rho.copy ()
        rho_t[0] = rho_t[1] = rho_avg

        # For 0 <= ratio < 1 and 0 <= rho, correct spin density using on-top density
        nderiv_R = Pi.shape[0] if _fn_deriv else 1
        R = self.get_ratio (Pi[0:nderiv_R,:], rho_avg[0:nderiv_R,:])
        zeta = self.get_zeta (R, fn_deriv=_fn_deriv)
    
        # Chain rule!
        w = rho_avg * zeta[0:1]
        rho_t[0] += w
        rho_t[1] -= w

        if _fn_deriv > 0: return rho_t, R, zeta
        return rho_t

    def get_zeta (self, R, fn_deriv=0, _Rmax=1):
        r''' Compute the intermediate zeta used to compute the translated spin
        densities and its functional derivatives 

        From the original translation [Li Manni et al., JCTC 10, 3669 (2014)]:
        zeta = (1-ratio)^(1/2) ; ratio < 1
             = 0               ; otherwise

        Args:
            R : ndarray of shape (*,ngrids)
                Ratio (4Pi/rho^2) and possibly its spatial derivatives
                Only the first row is used in this function

        Kwargs:
            fn_deriv : integer
                order of functional derivative (d^n z / dR^n) to return
                along with the value of zeta
            _Rmax : float
                maximum value of R for which to compute zeta or its 
                derivatives; columns of zeta with R[0]>_Rmax are zero.
                This is a hook for the ``fully-translated'' child class
                and should not be touched normally.

        Returns:
            zeta : ndarray of shape (fn_deriv+1, ngrids)
        '''
        ngrids = R.shape[1]
        zeta = np.zeros ((fn_deriv+1, ngrids), dtype=R.dtype)
        idx = R[0] < _Rmax
        zeta[0,idx] = np.sqrt (1.0 - R[0,idx])
        if fn_deriv:
            zeta[1,idx] = -1 / zeta[0,idx] / 2
        if fn_deriv > 1: fac = 1 / (1.0-R[0,idx]) / 2
        for n in range (1,fn_deriv):
            zeta[n+1,idx] = zeta[n,idx] * (2*n-1) * fac
        return zeta

    def split_x_c (self):
        ''' Get one translated functional for just the exchange and one for just the correlation part of the energy. '''
        if not re.search (',', self.otxc):
            x_code = c_code = self.otxc
            c_code = c_code[1:]
        else:
            x_code, c_code = ','.split (self.otxc)
        x_code = x_code + ','
        c_code = 't,' + c_code
        xfnal = copy.copy (self)
        xfnal._numint = copy.copy (self._numint)
        xfnal.grids = copy.copy (self.grids)
        xfnal.verbose = self.verbose
        xfnal.stdout = self.stdout
        xfnal.otxc = x_code
        cfnal = copy.copy (self)
        cfnal._numint = copy.copy (self._numint)
        cfnal.grids = copy.copy (self.grids)
        cfnal.verbose = self.verbose
        cfnal.stdout = self.stdout
        cfnal.otxc = c_code
        return xfnal, cfnal

    def jTx_op (self, rho, Pi, x, Rmax=1, zeta_deriv=False):
        r''' Evaluate jTx = (x.j)T where j is the Jacobian of the translated densities
        in terms of the untranslated density and pair density

        Args:
            rho : ndarray of shape (2,*,ngrids)
                containing spin-density [and derivatives]
            Pi : ndarray with shape (*,ngrids)
                containing on-top pair density [and derivatives]
            x : ndarray of shape (2,*,ngrids)
                Usually, a functional derivative of the on-top xc energy wrt
                translated densities

        Kwargs:
            Rmax : float
                For ratios above this value, rho is not translated and therefore
                the effective potential kernel is the same as in standard KS-DFT
            zeta_deriv : logical
                If true, propagate derivatives through the zeta intermediate as in
                ``fully-translated'' PDFT

        Returns: ndarray of shape (*,ngrids)
            Usually, a functional derivative of the on-top pair density exchange-correlation
            energy wrt to total density and its derivatives
            The potential must be spin-symmetric in pair-density functional theory
        '''
        return tfnal_derivs.jTx_op_transl (self, rho, Pi, x,
            Rmax=Rmax, zeta_deriv=zeta_deriv)


_FT_R0_DEFAULT=0.9
_FT_R1_DEFAULT=1.15
_FT_A_DEFAULT=-475.60656009
_FT_B_DEFAULT=-379.47331922 
_FT_C_DEFAULT=-85.38149682

class ftransfnal (transfnal):
    r''' "fully translated functional" of Carlson et al., JCTC 11, 4077 (2015)
    '''

    def __init__ (self, ks, **kwargs):
        otfnal.__init__(self, ks.mol, **kwargs)
        self.R0=_FT_R0_DEFAULT
        self.R1=_FT_R1_DEFAULT
        self.A=_FT_A_DEFAULT
        self.B=_FT_B_DEFAULT
        self.C=_FT_C_DEFAULT
        self.otxc = 'ft' + ks.xc
        self._numint = copy.copy (ks._numint)
        self.grids = copy.copy (ks.grids)
        self._numint.hybrid_coeff = ft_hybrid_coeff.__get__(self._numint)
        self._numint.nlc_coeff = ft_nlc_coeff.__get__(self._numint)
        self._numint.rsh_coeff = ft_rsh_coeff.__get__(self._numint)
        self._numint.eval_xc = ft_eval_xc.__get__(self._numint)
        self._numint._xc_type = ft_xc_type.__get__(self._numint)
        self._init_info ()

    Pi_deriv = 1

    def eval_ot (self, rho, Pi, dderiv=0, weights=None):
        if dderiv>0:
            raise NotImplementedError ("density derivatives for fully-translated functionals")
        return transfnal.eval_ot (self, rho, Pi, dderiv=dderiv, weights=weights)

    def get_rho_translated (self, Pi, rho, **kwargs):
        r''' Compute the "fully-translated" alpha and beta densities
        and their derivatives. This is the same as "translated" except

        rho'_t^a += zeta' * rho / 2
        rho'_t^b -= zeta' * rho / 2

        And the functional form of "zeta" is changed (see "get_zeta")
    
        Args:
            Pi : ndarray of shape (*, ngrids)
                containing on-top pair density [and derivatives]
            rho : ndarray of shape (2, *, ngrids)
                containing spin density [and derivatives]
    
        Returns: 
            rho_ft : ndarray of shape (2,*,ngrids)
                Fully-translated spin density (and derivatives)
        '''
        nderiv_R = max (rho.shape[1], Pi.shape[0])
        if nderiv_R == 1: return transfnal.get_rho_translated (self, Pi, rho)

        # Spin density and first term of spin gradient in common with transfnal    
        rho_avg = (rho[0,:,:] + rho[1,:,:]) / 2
        rho_ft, R, zeta = transfnal.get_rho_translated (self, Pi, rho, _fn_deriv=1)

        # Add propagation of chain rule through zeta
        w = (rho_avg[0] * zeta[1])[None,:] * R[1:4]
        rho_ft[0][1:4] += w
        rho_ft[1][1:4] -= w

        return np.squeeze (rho_ft)

    def get_zeta (self, R, fn_deriv=1, **kwargs):
        r''' Compute the intermediate zeta used to compute the translated spin
        densities and its functional derivatives 

        From the "full" translation [Carlson et al., JCTC 11, 4077 (2015)]:
        zeta = (1-R)^(1/2)                          ; R < R0
             = A*(R-R1)^5 + B*(R-R1)^4 + C*(R-R1)^3 ; R0 <= R < R1
             = 0                                    ; otherwise

        Args:
            R : ndarray of shape (*,ngrids)
                Ratio (4Pi/rho^2) and possibly its spatial derivatives
                Only the first row is used in this function

        Kwargs:
            fn_deriv : integer
                order of functional derivative (d^n z / dR^n) to return
                along with the value of zeta

        Returns:
            zeta : ndarray of shape (fn_deriv+1, ngrids)
        '''
        # Rmax unused here. It only needs to be passed in the transfnal version
        R0, R1, A, B, C = self.R0, self.R1, self.A, self.B, self.C
        zeta = transfnal.get_zeta (self, R, fn_deriv=fn_deriv, _Rmax=R0)
        idx = (R[0] >= R0) & (R[0] < R1)
        if not np.count_nonzero (idx): return zeta
        zeta[:,idx] = 0.0
        dR = np.stack ([np.power (R[0,idx] - R1, n)
            for n in range (1,6)], axis=0)
        def _derivs ():
            yield     A*dR[4] +    B*dR[3] +   C*dR[2]
            yield   5*A*dR[3] +  4*B*dR[2] + 3*C*dR[1]
            yield  20*A*dR[2] + 12*B*dR[1] + 6*C*dR[0]
            yield  60*A*dR[1] + 24*B*dR[0] + 6*C
            yield 120*A*dR[0] + 24*B
            yield 120*A
        for n, row in enumerate (_derivs ()):
            zeta[n,idx] = row
            if n == fn_deriv: break

        return zeta

    def split_x_c (self):
        xfnal, cfnal = super().split_x_c ()
        xfnal.otxc = 'f' + xfnal.otxc
        cfnal.otxc = 'f' + cfnal.otxc
        return xfnal, cfnal


_CS_a_DEFAULT = 0.04918
_CS_b_DEFAULT = 0.132
_CS_c_DEFAULT = 0.2533
_CS_d_DEFAULT = 0.349

class colle_salvetti_corr (otfnal):


    def __init__(self, mol, **kwargs):
        super().__init__(mol, **kwargs)
        self.otxc = 'Colle_Salvetti'
        self._numint = NumInt ()
        self.grids = Grids (mol)
        self._numint.hybrid_coeff = lambda * args : 0
        self._numint.nlc_coeff = lambda * args : [0, 0]
        self._numint.rsh_coeff = lambda * args : [0, 0, 0]
        self._numint._xc_type = lambda * args : 'MGGA'
        self.CS_a =_CS_a_DEFAULT
        self.CS_b =_CS_b_DEFAULT
        self.CS_c =_CS_c_DEFAULT
        self.CS_d =_CS_d_DEFAULT 
        self._init_info ()

    def get_E_ot (self, rho, Pi, weight):
        r''' Colle & Salvetti, Theor. Chim. Acta 37, 329 (1975)
        see also Lee, Yang, Parr, Phys. Rev. B 37, 785 (1988) [Eq. (3)]'''

        a, b, c, d = self.CS_a, self.CS_b, self.CS_c, self.CS_d
        rho_tot = rho[0,0] + rho[1,0]
        idx = rho_tot > 1e-15

        num  = -c * np.power (rho_tot[idx], -1/3)
        num  = np.exp (num, num)
        num *= Pi[4,idx]
        num *= b * np.power (rho_tot[idx], -8/3)
        num += 1

        denom  = d * np.power (rho_tot[idx], -1/3)
        denom += 1

        num /= denom
        num *= Pi[0,idx]
        num /= rho_tot[idx]
        num *= weight[idx]

        E_ot  = np.sum (num)
        E_ot *= -4 * a
        return E_ot      
                

def ft_continuity_debug (ot, R, rho, zeta, R0, R1, nrows=50):
    r''' Not working - I need to rethink this '''
    idx = np.argsort (np.abs (R - R0))
    logger.debug (ot, "Close to R0 (%s)", R0)
    logger.debug (ot, "{:19s} {:19s} {:19s} {:19s} {:19s} {:19s} {:19s}".format ("R", "rho_a", "rho_b", "zeta", "zeta_x", "zeta_y", "zeta_z"))
    for irow in idx[:nrows]:
        debugstr = "{:19.12e} {:19.12e} {:19.12e} {:19.12e} {:19.12e} {:19.12e} {:19.12e}".format (R[irow], *rho[:,irow], *zeta[:,irow]) 
        logger.debug (ot, debugstr)
    idx = np.argsort (np.abs (R - R1))
    logger.debug (ot, "Close to R1 (%s)", R1)
    logger.debug (ot, "{:19s} {:19s} {:19s} {:19s} {:19s} {:19s} {:19s}".format ("R", "rho_a", "rho_b", "zeta", "zeta_x", "zeta_y", "zeta_z"))
    for irow in idx[:nrows]:
        debugstr = "{:19.12e} {:19.12e} {:19.12e} {:19.12e} {:19.12e} {:19.12e} {:19.12e}".format (R[irow], *rho[:,irow], *zeta[:,irow]) 
        logger.debug (ot, debugstr)
    

def hybrid_2c_coeff (ni, xc_code, spin=0):
    ''' Wrapper to the xc_code hybrid coefficient parser to return the exchange and correlation components of the hybrid coefficent separately '''

    # For all prebuilt and exchange-only functionals, hyb_c = 0
    if not re.search (',', xc_code): return [_NumInt.hybrid_coeff(ni, xc_code, spin=0), 0]

    # All factors of 'HF' are summed by default. Therefore just run the same code for the exchange and correlation parts of the string separately
    x_code, c_code = xc_code.split (',')
    c_code = ',' + c_code
    hyb_x = _NumInt.hybrid_coeff(ni, x_code, spin=0) if len (x_code) else 0
    hyb_c = _NumInt.hybrid_coeff(ni, c_code, spin=0) if len (c_code) else 0
    return [hyb_x, hyb_c]

def make_scaled_fnal (xc_code, hyb_x = 0, hyb_c = 0, fnal_x = None, fnal_c = None):
    ''' Convenience function to write the xc_code corresponding to a functional of the type

        Exc = hyb_x*E_x[Psi] + fnal_x*E_x[rho] + hyb_c*E_c[Psi] + fnal_c*E_c[rho]

        where E[Psi] is an energy from a wave function, and E[rho] is a density functional from libxc.
        The decomposition of E[Psi] into exchange (E_x) and correlation (E_c) components is arbitrary.

        Args:
            xc_code : string
                As used in pyscf.dft.libxc. If it contains no comma, it is assumed to be a predefined functional
                with separately-defined exchange and correlation parts: 'xc_code' -> 'xc_code,xc_code'. 
                Currently cannot parse mixed functionals.

        Kwargs:
            hyb_x : float
                fraction of wave function exchange to be included in the functional
            hyb_c : float
                fraction of wave function correlation to be included in the functional
            fnal_x : float
                fraction of density functional exchange to be included. Defaults to 1 - hyb_x.
            fnal_c : float
                fraction of density functional correlation to be included. Defaults to 1 - hyb_c.

        returns:
            xc_code : string
                If xc_code has exchange part x_code and correlation part c_code, the return value is
                'fnal_x * x_code + hyb_x * HF, fnal_c * c_code + hyb_c * HF'
                You STILL HAVE TO PREPEND 't' OR 'ft'!!!
    '''
    if fnal_x is None: fnal_x = 1 - hyb_x
    if fnal_c is None: fnal_c = 1 - hyb_c

    if not re.search (',', xc_code):
        x_code = c_code = xc_code
    else:
        x_code, c_code = ','.split (xc_code)

    # TODO: actually parse the xc_code so that custom functionals are compatible with this

    if fnal_x != 1:
        x_code = '{:.16f}*{:s}'.format (fnal_x, x_code)
    if hyb_x != 0:
        x_code = x_code + ' + {:.16f}*HF'.format (hyb_x)

    if fnal_c != 1:
        c_code = '{:.16f}*{:s}'.format (fnal_c, c_code)
    if hyb_c != 0:
        c_code = c_code + ' + {:.16f}*HF'.format (hyb_c)

    return x_code + ',' + c_code

def make_hybrid_fnal (xc_code, hyb, hyb_type = 4):
    ''' Convenience function to write "hybrid" xc functional in terms of only one parameter

        Args:
            xc_code : string
                As used in pyscf.dft.libxc. If it contains no comma, it is assumed to be a predefined functional
                with separately-defined exchange and correlation parts: 'xc_code' -> 'xc_code,xc_code'. 
                Currently cannot parse mixed functionals.
            hyb : float
                Parameter(s) defining the "hybridization" which is handled in various ways according to hyb_type

        Kwargs:
            hyb_type : int or string
                The type of hybrid functionals to construct. Current options are:
                - 0 or 'translation': Hybrid fnal is 'hyb*HF + (1-hyb)*x_code, hyb*HF + c_code'.
                    Based on the idea that 'exact exchange' of the translated functional
                    corresponds to exchange plus correlation energy of the underlying wave function.
                    Requires len (hyb) == 1.
                - 1 or 'average': Hybrid fnal is 'hyb*HF + (1-hyb)*x_code, hyb*HF + (1-hyb)*c_code'.
                    Based on the idea that hyb = 1 recovers the wave function energy itself.
                    Requires len (hyb) == 1.
                - 2 or 'diagram': Hybrid fnal is 'hyb*HF + (1-hyb)*x_code, c_code'.
                    Based on the idea that the exchange energy of the wave function somehow can
                    be meaningfully separated from the correlation energy.
                    Requires len (hyb) == 1.
                - 3 or 'lambda': as in arXiv:1911.11162v1. Based on existing 'double-hybrid' functionals.
                    Requires len (hyb) == 1.
                - 4 or 'scaling': Hybrid fnal is 'a*HF + (1-a)*x_code, a*HF + (1-a**b)*c_code'
                    where a = hyb[0] and b = 1 + hyb[1]. Based on the scaling inequalities proven by 
                    Levy and Perdew in PRA 32, 2010 (1985):
                    E_c[rho_a] < a*E_c[rho] if a < 1 and
                    E_c[rho_a] > a*E_c[rho] if a > 1; 
                    BUT 
                    E_c[rho_a] ~/~ a^2 E_c[rho], implying that
                    E_c[rho_a] ~ a^b E_c[rho] with b > 1 unknown.
                    Requires len (hyb) == 2.
    '''

    if not hasattr (hyb, '__len__'): hyb = [hyb]
    HYB_TYPE_CODE = {'translation': 0,
                     'average':     1,
                     'diagram':     2,
                     'lambda':      3,
                     'scaling':     4}
    if isinstance (hyb_type, str): hyb_type = HYB_TYPE_CODE[hyb_type]

    if hyb_type == 0:
        assert (len (hyb) == 1)
        return make_scaled_fnal (xc_code, hyb_x=hyb[0], hyb_c=hyb[0], fnal_x=(1-hyb[0]), fnal_c=1)
    elif hyb_type == 1:
        assert (len (hyb) == 1)
        return make_scaled_fnal (xc_code, hyb_x=hyb[0], hyb_c=hyb[0], fnal_x=(1-hyb[0]), fnal_c=(1-hyb[0]))
    elif hyb_type == 2:
        assert (len (hyb) == 1)
        return make_scaled_fnal (xc_code, hyb_x=hyb[0], hyb_c=0, fnal_x=(1-hyb[0]), fnal_c=1)
    elif hyb_type == 3:
        assert (len (hyb) == 1)
        return make_scaled_fnal (xc_code, hyb_x=hyb[0], hyb_c=hyb[0], fnal_x=(1-hyb[0]), fnal_c=(1-(hyb[0]*hyb[0])))
    elif hyb_type == 4:
        assert (len (hyb) == 2)
        a = hyb[0]
        b = hyb[0]**(1+hyb[1])
        return make_scaled_fnal (xc_code, hyb_x=a, hyb_c=a, fnal_x=(1-a), fnal_c=(1-b))
    else:
        raise RuntimeError ('hybrid type undefined')

__t_doc__ = "For 'translated' functionals, otxc string = 't' + xc string\n"
__ft_doc__ = "For 'fully translated' functionals, otxc string = 'ft' + xc string\n"

def t_hybrid_coeff(ni, xc_code, spin=0):
    #return _NumInt.hybrid_coeff(ni, xc_code[1:], spin=0)
    return hybrid_2c_coeff (ni, xc_code[1:], spin=0)
t_hybrid_coeff.__doc__ = __t_doc__ + str(_NumInt.hybrid_coeff.__doc__)

def t_nlc_coeff(ni, xc_code):
    return _NumInt.nlc_coeff(ni, xc_code[1:])
t_nlc_coeff.__doc__ = __t_doc__ + str(_NumInt.nlc_coeff.__doc__)

def t_rsh_coeff(ni, xc_code):
    return _NumInt.rsh_coeff(ni, xc_code[1:])
t_rsh_coeff.__doc__ = __t_doc__ + str(_NumInt.rsh_coeff.__doc__)

def t_eval_xc(ni, xc_code, rho, spin=0, relativity=0, deriv=1, verbose=None):
    return _NumInt.eval_xc(ni, xc_code[1:], rho, spin=spin, relativity=relativity, deriv=deriv, verbose=verbose)
t_eval_xc.__doc__ = __t_doc__ + str(_NumInt.eval_xc.__doc__)

def t_xc_type(ni, xc_code):
    return _NumInt._xc_type(ni, xc_code[1:])
t_xc_type.__doc__ = __t_doc__ + str(_NumInt._xc_type.__doc__)

def t_rsh_and_hybrid_coeff(ni, xc_code, spin=0):
    return _NumInt.rsh_and_hybrid_coeff (ni, xc_code[1:], spin=spin)
t_rsh_and_hybrid_coeff.__doc__ = __t_doc__ + str(_NumInt.rsh_and_hybrid_coeff.__doc__)

def ft_hybrid_coeff(ni, xc_code, spin=0):
    #return _NumInt.hybrid_coeff(ni, xc_code[2:], spin=0)
    return hybrid_2c_coeff(ni, xc_code[2:], spin=0)
ft_hybrid_coeff.__doc__ = __ft_doc__ + str(_NumInt.hybrid_coeff.__doc__)

def ft_nlc_coeff(ni, xc_code):
    return _NumInt.nlc_coeff(ni, xc_code[2:])
ft_nlc_coeff.__doc__ = __ft_doc__ + str(_NumInt.nlc_coeff.__doc__)

def ft_rsh_coeff(ni, xc_code):
    return _NumInt.rsh_coeff(ni, xc_code[2:])
ft_rsh_coeff.__doc__ = __ft_doc__ + str(_NumInt.rsh_coeff.__doc__)

def ft_eval_xc(ni, xc_code, rho, spin=0, relativity=0, deriv=1, verbose=None):
    return _NumInt.eval_xc(ni, xc_code[2:], rho, spin=spin, relativity=relativity, deriv=deriv, verbose=verbose)
ft_eval_xc.__doc__ = __ft_doc__ + str(_NumInt.eval_xc.__doc__)

def ft_xc_type(ni, xc_code):
    return _NumInt._xc_type(ni, xc_code[2:])
ft_xc_type.__doc__ = __ft_doc__ + str(_NumInt._xc_type.__doc__)

def ft_rsh_and_hybrid_coeff(ni, xc_code, spin=0):
    return _NumInt.rsh_and_hybrid_coeff (ni, xc_code[2:], spin=spin)
ft_rsh_and_hybrid_coeff.__doc__ = __ft_doc__ + str(_NumInt.rsh_and_hybrid_coeff.__doc__)

