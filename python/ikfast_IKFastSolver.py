from ikfast import fmod, atan2check, clc, ikfast_print_stack, ipython_str, \
    print_matrix, LOGGING_FORMAT

from sympy import __version__ as sympy_version
if sympy_version < '0.7.0':
    raise ImportError('ikfast needs sympy 0.7.x or greater')
sympy_smaller_073 = sympy_version < '0.7.3'

__author__    = 'Rosen Diankov'
__copyright__ = 'Copyright (C) 2009-2012 Rosen Diankov <rosen.diankov@gmail.com>'
__license__   = 'Lesser GPL, Version 3'
__version__   = '0x1000004a' # hex of the version, has to be prefixed with 0x. also in ikfast.h

import sys, copy, time, math, datetime
import __builtin__

from optparse import OptionParser
try:
    from openravepy.metaclass import AutoReloader
    from openravepy import axisAngleFromRotationMatrix
except:
    axisAngleFromRotationMatrix = None
    class AutoReloader:
        pass

import numpy # required for fast eigenvalue computation
from sympy import *
if sympy_version > '0.7.1':
    _zeros, _ones = zeros, ones
    zeros = lambda args: _zeros(*args)
    ones  = lambda args: _ones(*args)
    
try:
    import mpmath # on some distributions, sympy does not have mpmath in its scope
except ImportError:
    pass

try:
    import re # for latex cleanup
except ImportError:
    pass

try:
    from math import isinf, isnan
except ImportError:
    # python 2.5 
    from numpy import isinf as _isinf
    from numpy import isnan as _isnan
    def isinf(x): return _isinf(float(x))
    def isnan(x): return _isnan(float(x))

from operator import itemgetter, mul
# mul for reduce(mul, [...], 1), same as prod([...]) in Matlab

from itertools import izip, chain, product
try:
    from itertools import combinations, permutations
except ImportError:
    def combinations(items,n):
        if n == 0: yield[]
        else:
            _internal_items=list(items)
            for  i in xrange(len(_internal_items)):
                for cc in combinations(_internal_items[i+1:],n-1):
                    yield [_internal_items[i]]+cc

import logging
logging.basicConfig( format = LOGGING_FORMAT, \
                     datefmt='%d-%m-%Y:%H:%M:%S', \
                     level=logging.DEBUG)
log = logging.getLogger('IKFastSolver')
hdlr = logging.FileHandler('/var/tmp/ikfast_IKFastSolver.log')
formatter = logging.Formatter(LOGGING_FORMAT)
hdlr.setFormatter(formatter)
log.addHandler(hdlr)

try:
    # not necessary, just used for testing
    import swiginac
    using_swiginac = True
except ImportError:
    using_swiginac = False

CodeGenerators = {}
try:
    import ikfast_generator_cpp
    CodeGenerators['cpp'] = ikfast_generator_cpp.CodeGenerator
    IkType = ikfast_generator_cpp.IkType
except ImportError:
    pass
# try:
#     import ikfast_generator_vb
#     CodeGenerators['vb'] = ikfast_generator_vb.CodeGenerator
#     CodeGenerators['vb6'] = ikfast_generator_vb.CodeGeneratorVB6
#     CodeGenerators['vb6special'] = ikfast_generator_vb.CodeGeneratorVB6Special
# except ImportError:
#     pass

class IKFastSolver(AutoReloader):
    """Solves the analytical inverse kinematics equations. The symbol naming conventions are as follows:

    cjX - cos joint angle
    constX - temporary constant used to simplify computations    
    dummyX - dummy intermediate variables to solve for
    gconstX - global constant that is also used during ik generation phase
    htjX - half tan of joint angle
    jX - joint angle
    pX - end effector position information
    rX - end effector rotation information
    sjX - sin joint angle
    tconstX - second-level temporary constant
    tjX - tan of joint angle    
    """

    class CannotSolveError(Exception):
        """thrown when ikfast fails to solve a particular set of equations with the given knowns and unknowns
        """
        def __init__(self,value=u''):
            self.value = value
        def __unicode__(self):
            return u'%s: %s'%(self.__class__.__name__, self.value)
        
        def __str__(self):
            return unicode(self).encode('utf-8')
        
        def __repr__(self):
            return '<%s(%r)>'%(self.__class__.__name__, self.value)
        
        def __eq__(self, r):
            return self.value == r.value
        
        def __ne__(self, r):
            return self.value != r.value
        
    class IKFeasibilityError(Exception):
        """thrown when it is not possible to solve the IK due to robot not having enough degrees of freedom. For example, a robot with 5 joints does not have 6D IK
        """
        def __init__(self,equations,checkvars):
            self.equations=equations
            self.checkvars=checkvars
        def __str__(self):
            s = "Not enough equations to solve for variables %s!\n" + \
                "This means that EITHER\n" + \
                "- there are not enough constraints to solve for all variables, OR\n" + \
                "- the manipulator does not span the target IK space.\n" + \
                "This is not an IKFast's failure; it just means the robot kinematics are invalid for this type of IK.\n" + \
                "Equations that are not uniquely solvable are:\n" % str(self.checkvars)
            for eq in self.equations:
                s += str(eq) + '\n'
            return s

    class JointAxis:
        __slots__ = ['joint','iaxis']

    class Variable:
        __slots__ = ['name','var','svar','cvar','tvar','htvar','vars','subs','subsinv']
        def __init__(self, var):
            self.name = var.name
            self.var = var
            self.svar = Symbol("s%s"%var.name)
            self.cvar = Symbol("c%s"%var.name)
            self.tvar = Symbol("t%s"%var.name)
            self.htvar = Symbol("ht%s"%var.name)
            self.vars = [self.var,self.svar,self.cvar,self.tvar,self.htvar]
            self.subs = [(cos(self.var),self.cvar),(sin(self.var),self.svar),(tan(self.var),self.tvar),(tan(self.var/2),self.htvar)]
            self.subsinv = [(self.cvar,cos(self.var)),(self.svar, sin(self.var)),(self.tvar,tan(self.tvar))]
        def getsubs(self,value):
            return [(self.var,value)]+[(s,v.subs(self.var,value).evalf()) for v,s in self.subs]

    class DegenerateCases:
        def __init__(self):
            self.handleddegeneratecases = []
        def Clone(self):
            clone=IKFastSolver.DegenerateCases()
            clone.handleddegeneratecases = self.handleddegeneratecases[:]
            return clone
        def AddCasesWithConditions(self,newconds,currentcases):
            for case in newconds:
                newcases = set(currentcases)
                newcases.add(case)
                assert(not self.CheckCases(newcases))
                self.handleddegeneratecases.append(newcases)
        def AddCases(self,currentcases):
            if not self.CheckCases(currentcases):
                self.handleddegeneratecases.append(currentcases)
            else:
                log.warn('case already added') # sometimes this can happen, but it isn't a bug, just bad bookkeeping
        def RemoveCases(self, currentcases):
            for i, handledcases in enumerate(self.handleddegeneratecases):
                if handledcases == currentcases:
                    self.handleddegeneratecases.pop(i)
                    return True
            return False
        def GetHandledConditions(self,currentcases):
            handledconds = []
            for handledcases in self.handleddegeneratecases:
                if len(currentcases)+1==len(handledcases) and currentcases < handledcases:
                    handledconds.append((handledcases - currentcases).pop())
            return handledconds
        def CheckCases(self,currentcases):
            for handledcases in self.handleddegeneratecases:
                if handledcases == currentcases:
                    return True
            return False
    
    def __init__(self, kinbody=None,kinematicshash='',precision=None, checkpreemptfn=None):
        """
        :param checkpreemptfn: checkpreemptfn(msg, progress) called periodically at various points in ikfast. Takes in two arguments to notify user how far the process has completed.
        """
        self._checkpreemptfn = checkpreemptfn
        self.usinglapack = False
        self.useleftmultiply = True
        self.freevarsubs = []
        self.degeneratecases = None
        self.kinematicshash = kinematicshash
        self.testconsistentvalues = None
        self.maxcasedepth = 3 # the maximum depth of special/degenerate cases to process before system gives up
        self.globalsymbols = [] # global symbols for substitutions
        self._scopecounter = 0 # a counter for debugging purposes that increaes every time a level changes
        self._dodebug = False
        self._ikfastoptions = 0
        if precision is None:
            self.precision=8
        else:
            self.precision=precision
        self.kinbody = kinbody
        self._iktype = None # the current iktype processing
        self.axismap = {}
        self.axismapinv = {}
        with self.kinbody:
            for idof in range(self.kinbody.GetDOF()):
                axis = IKFastSolver.JointAxis()
                axis.joint = self.kinbody.GetJointFromDOFIndex(idof)
                axis.iaxis = idof-axis.joint.GetDOFIndex()
                name = str('j%d')%idof
                self.axismap[name] = axis
                self.axismapinv[idof] = name

        # TGN adds the following
        self.trigvars_subs = [];
        self.trigsubs = [];
    
    def _CheckPreemptFn(self, msg = u'', progress = 0.25):
        """
        Progress is in [0,1], when 0 means "start" and 1 means "finish"
        """
        if self._checkpreemptfn is not None:
            self._checkpreemptfn(msg, progress = progress)
    
    def convertRealToRational(self, x,precision=None):
        if precision is None:
            precision=self.precision
        if Abs(x) < 10**-precision:
            return S.Zero
        r0 = Rational(str(round(Float(float(x),30),precision)))
        if x == 0:
            return r0
        r1 = 1/Rational(str(round(Float(1/float(x),30),precision)))
        return r0 if len(str(r0)) < len(str(r1)) else r1

    def ConvertRealToRationalEquation(self, eq, precision=None):
        if eq.is_Add:
            neweq = S.Zero
            for subeq in eq.args:
                neweq += self.ConvertRealToRationalEquation(subeq,precision)
        elif eq.is_Mul:
            neweq = self.ConvertRealToRationalEquation(eq.args[0],precision)
            for subeq in eq.args[1:]:
                neweq *= self.ConvertRealToRationalEquation(subeq,precision)
        elif eq.is_Function:
            newargs = [self.ConvertRealToRationalEquation(subeq,precision) for subeq in eq.args]
            neweq = eq.func(*newargs)
        elif eq.is_number:
            if eq.is_irrational:
                # don't touch it since it could be pi!
                neweq = eq
            else:
                neweq = self.convertRealToRational(eq,precision)
        else:
            neweq=eq
        return neweq
    
    def normalizeRotation(self,M):
        """error from openrave can be on the order of 1e-6 (especially if they are defined diagonal to some axis)
        """
        right = Matrix(3,1,[self.convertRealToRational(x,self.precision-3) for x in M[0,0:3]])
        right = right/right.norm()
        up = Matrix(3,1,[self.convertRealToRational(x,self.precision-3) for x in M[1,0:3]])
        up = up - right*right.dot(up)
        up = up/up.norm()
        d = right.cross(up)
        for i in range(3):
            # don't round the rotational part anymore since it could lead to unnormalized rotations!
            M[0,i] = right[i]
            M[1,i] = up[i]
            M[2,i] = d[i]
            M[i,3] = self.convertRealToRational(M[i,3])
            M[3,i] = S.Zero
        M[3,3] = S.One
        return M
    
    def GetMatrixFromNumpy(self,T):
        return Matrix(4,4,[x for x in T.flat])
    
    def RoundMatrix(self, T):
        """given a sympy matrix, will round the matrix and snap all its values to 15, 30, 45, 60, and 90 degrees.
        """
        if axisAngleFromRotationMatrix is not None:
            Teval = T.evalf()
            axisangle = axisAngleFromRotationMatrix([[Teval[0,0], Teval[0,1], Teval[0,2]], [Teval[1,0], Teval[1,1], Teval[1,2]], [Teval[2,0], Teval[2,1], Teval[2,2]]])
            angle = sqrt(axisangle[0]**2+axisangle[1]**2+axisangle[2]**2)
            if abs(angle) < 10**(-self.precision):
                # rotation is identity
                M = eye(4)
            else:
                axisangle = axisangle/angle
                log.debug('rotation angle: %f, axis=[%f,%f,%f]', (angle*180/pi).evalf(),axisangle[0],axisangle[1],axisangle[2])
                accurateaxisangle = Matrix(3,1,[self.convertRealToRational(x,self.precision-3) for x in axisangle])
                accurateaxisangle = accurateaxisangle/accurateaxisangle.norm()
                # angle is not a multiple of 90, can get long fractions. so check if there's any way to simplify it
                if abs(angle-3*pi/2) < 10**(-self.precision+2):
                    quat = [-S.One/sqrt(2), accurateaxisangle[0]/sqrt(2), accurateaxisangle[1]/sqrt(2), accurateaxisangle[2]/sqrt(2)]
                elif abs(angle-pi) < 10**(-self.precision+2):
                    quat = [S.Zero, accurateaxisangle[0], accurateaxisangle[1], accurateaxisangle[2]]
                elif abs(angle-2*pi/3) < 10**(-self.precision+2):
                    quat = [Rational(1,2), accurateaxisangle[0]*sqrt(3)/2, accurateaxisangle[1]*sqrt(3)/2, accurateaxisangle[2]*sqrt(3)/2]
                elif abs(angle-pi/2) < 10**(-self.precision+2):
                    quat = [S.One/sqrt(2), accurateaxisangle[0]/sqrt(2), accurateaxisangle[1]/sqrt(2), accurateaxisangle[2]/sqrt(2)]
                elif abs(angle-pi/3) < 10**(-self.precision+2):
                    quat = [sqrt(3)/2, accurateaxisangle[0]/2, accurateaxisangle[1]/2, accurateaxisangle[2]/2]
                elif abs(angle-pi/4) < 10**(-self.precision+2):
                    # cos(pi/8) = sqrt(sqrt(2)+2)/2
                    # sin(pi/8) = sqrt(-sqrt(2)+2)/2
                    quat = [sqrt(sqrt(2)+2)/2, sqrt(-sqrt(2)+2)/2*accurateaxisangle[0], sqrt(-sqrt(2)+2)/2*accurateaxisangle[1], sqrt(-sqrt(2)+2)/2*accurateaxisangle[2]]
                elif abs(angle-pi/6) < 10**(-self.precision+2):
                 # cos(pi/12) = sqrt(2)/4+sqrt(6)/4
                    # sin(pi/12) = -sqrt(2)/4+sqrt(6)/4
                    quat = [sqrt(2)/4+sqrt(6)/4, (-sqrt(2)/4+sqrt(6)/4)*accurateaxisangle[0], (-sqrt(2)/4+sqrt(6)/4)*accurateaxisangle[1], (-sqrt(2)/4+sqrt(6)/4)*accurateaxisangle[2]]
                else:
                    # could not simplify further
                    #assert(0)
                    return self.normalizeRotation(T)
                
                M = self.GetMatrixFromQuat(quat)
            for i in range(3):
                M[i,3] = self.convertRealToRational(T[i,3],self.precision)
            return M
        
        if isinstance(T, Matrix):
            return self.normalizeRotation(Matrix(4,4,[x for x in T]))
        else:
            return self.normalizeRotation(Matrix(4,4,[x for x in T.flat]))
        
    def numpyVectorToSympy(self,v,precision=None):
        return Matrix(len(v),1,[self.convertRealToRational(x,precision) for x in v])
    
    @staticmethod
    def rodrigues(axis, angle):
        return IKFastSolver.rodrigues2(axis,cos(angle),sin(angle))
    
    @staticmethod
    def GetMatrixFromQuat(quat):
        """
        Quaternion is [cos(angle/2), v*sin(angle/2)] with unit v

        Returns 4x4 matrix with rotation component set
        """
        M = eye(4)
        qq1 = 2*quat[1]*quat[1]
        qq2 = 2*quat[2]*quat[2]
        qq3 = 2*quat[3]*quat[3]
        M[0,0] = 1 - qq2 - qq3
        M[0,1] = 2*(quat[1]*quat[2] - quat[0]*quat[3])
        M[0,2] = 2*(quat[1]*quat[3] + quat[0]*quat[2])
        M[1,0] = 2*(quat[1]*quat[2] + quat[0]*quat[3])
        M[1,1]= 1 - qq1 - qq3
        M[1,2]= 2*(quat[2]*quat[3] - quat[0]*quat[1])
        M[2,0] = 2*(quat[1]*quat[3] - quat[0]*quat[2])
        M[2,1] = 2*(quat[2]*quat[3] + quat[0]*quat[1])
        M[2,2] = 1 - qq1 - qq2
        return M

    @staticmethod
    def rodrigues2(axis, cosangle, sinangle):
        skewsymmetric = Matrix(3, 3, [S.Zero,-axis[2],axis[1],axis[2],S.Zero,-axis[0],-axis[1],axis[0],S.Zero])
        return eye(3) + sinangle * skewsymmetric + (S.One-cosangle)*skewsymmetric*skewsymmetric

    @staticmethod
    def affineInverse(affinematrix):
        T = eye(4)
        affinematrix_transpose = affinematrix[0:3,0:3].transpose()
        T[0:3,0:3] =  affinematrix_transpose
        T[0:3,3]   = -affinematrix_transpose * affinematrix[0:3,3]
        return T

    @staticmethod
    def affineSimplify(T):
        return Matrix(T.shape[0],T.shape[1],[trigsimp(x.expand()) for x in T])

    @staticmethod
    def multiplyMatrix(Ts):
        if len(Ts)==0:
            return eye(4)
        else:
            return reduce(mul, Ts, 1)
    
    @staticmethod
    def equal(eq0, eq1):
        if isinstance(eq0, Poly):
            eq0 = eq0.as_expr()
        if isinstance(eq1, Poly):
            eq1 = eq1.as_expr()
        return eq0-eq1 == S.Zero # TGN: BOLD move, see if it works. expand(eq0-eq1) == S.Zero

    def chop(self, expr, precision = None):
        return expr

    def IsHinge(self, axisname):
        if axisname[0]!='j' or not axisname in self.axismap:
            if axisname == 'j100':
                # always revolute!
                # TGN: what's j100?
                return True
            
            log.info('IsHinge returns false for variable %s'% axisname)
            return False # dummy joint most likely for angles
        
        return self.axismap[axisname].joint.IsRevolute(self.axismap[axisname].iaxis)

    def IsPrismatic(self,axisname):
        if axisname[0]!='j' or not axisname in self.axismap:
            log.info('IsPrismatic returns false for variable %s' % axisname)
            return False # dummy joint most likely for angles
        
        return self.axismap[axisname].joint.IsPrismatic(self.axismap[axisname].iaxis)

    def forwardKinematicsChain(self, chainlinks, chainjoints):
        """
        The first and last matrices returned are always non-symbolic
        """
        with self.kinbody:
            assert(len(chainjoints)+1 == len(chainlinks))
            Links = []
            Tright = eye(4)
            jointvars = []
            jointinds = []
            for i,joint in enumerate(chainjoints):
                if len(joint.GetName()) == 0:
                    raise self.CannotSolveError('chain %s:%s contains a joint with no name!' \
                                                % (chainlinks[0].GetName(), \
                                                  chainlinks[-1].GetName()))
                
                if chainjoints[i].GetHierarchyParentLink() == chainlinks[i]:
                    TLeftjoint  = self.GetMatrixFromNumpy(joint.GetInternalHierarchyLeftTransform())
                    TRightjoint = self.GetMatrixFromNumpy(joint.GetInternalHierarchyRightTransform())
                    axissign = S.One
                else:
                    TLeftjoint  = self.affineInverse(self.GetMatrixFromNumpy(joint.GetInternalHierarchyRightTransform()))
                    TRightjoint = self.affineInverse(self.GetMatrixFromNumpy(joint.GetInternalHierarchyLeftTransform()))
                    axissign = -S.One
                    
                if joint.IsStatic():
                    Tright = self.affineSimplify(Tright * TLeftjoint * TRightjoint)
                else:
                    Tjoints = []
                    for iaxis in range(joint.GetDOF()):
                        var = None
                        if joint.GetDOFIndex() >= 0:
                            var = Symbol(self.axismapinv[joint.GetDOFIndex()])
                            cosvar = cos(var)
                            sinvar = sin(var)
                            jointvars.append(var)
                            
                        elif joint.IsMimic(iaxis):
                            # get the mimic equation
                            var = joint.GetMimicEquation(iaxis)
                            for itestjoint, testjoint in enumerate(chainjoints):
                                var = var.replace(testjoint.GetName(), 'j%d'%itestjoint)
                            # this needs to be reduced!
                            cosvar = cos(var)
                            sinvar = sin(var)
                            
                        elif joint.IsStatic():
                            # joint doesn't move so assume identity
                            pass
                        else:
                            raise ValueError('cannot solve for mechanism' + \
                                             'when a non-mimic passive joint %s is in chain' % str(joint))
                        
                        Tj = eye(4)
                        if var is not None:
                            jaxis = axissign * self.numpyVectorToSympy(joint.GetInternalHierarchyAxis(iaxis))
                            if joint.IsRevolute(iaxis):
                                Tj[0:3,0:3] = self.rodrigues2(jaxis, cosvar, sinvar)
                            elif joint.IsPrismatic(iaxis):
                                Tj[0:3,3] = jaxis*(var)
                            else:
                                raise ValueError('failed to process joint %s' % joint.GetName())
                        
                        Tjoints.append(Tj)
                    
                    if axisAngleFromRotationMatrix is not None:
                        axisangle = axisAngleFromRotationMatrix(\
                                                                numpy.array(\
                                                                            numpy.array(\
                                                                                        Tright * TLeftjoint), \
                                                                            numpy.float64))
                        
                        angle = sqrt(axisangle[0]**2 + axisangle[1]**2 + axisangle[2]**2)
                        
                        if angle > 1e-8:
                            axisangle = axisangle/angle

                        log.debug('rotation angle of Links[%d]: %f, ' + \
                                  'axis = [%f, %f, %f]', \
                                  len(Links), (angle*180/pi).evalf(), \
                                  axisangle[0], axisangle[1], axisangle[2])
                            
                    Links.append(self.RoundMatrix(Tright * TLeftjoint))
                    
                    for Tj in Tjoints:
                        jointinds.append(len(Links))
                        Links.append(Tj)
                        
                    Tright = TRightjoint
                    
            Links.append(self.RoundMatrix(Tright))
        
        # Before returning the final links, we try to push as much translation components
        # outward to both ends. Sometimes these components can get in the way of detecting
        # intersecting axes
        if len(jointinds) > 0:

            # TGN: this executes only once, so it is trivial to modify
            #
            # Better is to not multiply translation matrix, but to add values to a translation vector
            # 
            # TO-DO

            iright = jointinds[-1]
            Ttrans = eye(4)
            Ttrans[0:3,3] = Links[iright-1][0:3,0:3].transpose() * Links[iright-1][0:3,3]
            Trot_with_trans = Ttrans * Links[iright]
            separated_trans = Trot_with_trans[0:3,0:3].transpose() * Trot_with_trans[0:3,3]
            for j in range(0,3):
                if separated_trans[j].has(*jointvars):
                    Ttrans[j,3] = S.Zero
                else:
                    Ttrans[j,3] = separated_trans[j]
            Links[iright+1] = Ttrans * Links[iright+1]
            Links[iright-1] = Links[iright-1] * self.affineInverse(Ttrans)
            log.info("moved translation %s to right end",Ttrans[0:3,3].transpose())
            
        if len(jointinds) > 1:
            ileft = jointinds[0]
            separated_trans = Links[ileft][0:3,0:3] * Links[ileft+1][0:3,3]
            Ttrans = eye(4)
            for j in range(0,3):
                if not separated_trans[j].has(*jointvars):
                    Ttrans[j,3] = separated_trans[j]
            Links[ileft-1] = Links[ileft-1] * Ttrans
            Links[ileft+1] = self.affineInverse(Ttrans) * Links[ileft+1]
            log.info("moved translation %s to left end",Ttrans[0:3,3].transpose())
            
        if len(jointinds) > 3: # last 3 axes always have to be intersecting, move the translation of the first axis to the left
            ileft = jointinds[-3]
            separated_trans = Links[ileft][0:3,0:3] * Links[ileft+1][0:3,3]
            Ttrans = eye(4)
            for j in range(0,3):
                if not separated_trans[j].has(*jointvars):
                    Ttrans[j,3] = separated_trans[j]
            Links[ileft-1] = Links[ileft-1] * Ttrans
            Links[ileft+1] = self.affineInverse(Ttrans) * Links[ileft+1]
            log.info("moved translation on intersecting axis %s to left",Ttrans[0:3,3].transpose())
            
        return Links, jointvars
    
    def countVariables(self, expr, var):
        """
        Counts the number of terms in expr in which var appears
        """
        if expr.is_Add or expr.is_Mul: # TGN added expr.is_Mul
            return sum([1 for term in expr.args if term.has(var)])
        elif expr.has(var):
            return 1
        else:
            return 0
    
    @staticmethod
    def isValidPowers(expr):
        if expr.is_Pow:
            if not expr.exp.is_number or expr.exp < 0:
                return False
            return IKFastSolver.isValidPowers(expr.base)
        
        elif expr.is_Add or expr.is_Mul or expr.is_Function:
            return all([IKFastSolver.isValidPowers(arg) for arg in expr.args])
        
        else:
            return True
        
    @staticmethod
    def rotateDirection(sourcedir,targetdir):
        sourcedir /= sqrt(sourcedir.dot(sourcedir))
        targetdir /= sqrt(targetdir.dot(targetdir))
        rottodirection = sourcedir.cross(targetdir)
        fsin = sqrt(rottodirection.dot(rottodirection))
        fcos = sourcedir.dot(targetdir)
        M = eye(4)
        if fsin > 1e-6:
            M[0:3,0:3] = IKFastSolver.rodrigues(rottodirection*(1/fsin),atan2(fsin,fcos))
        elif fcos < 0: # hand is flipped 180, rotate around x axis
            rottodirection = Matrix(3,1,[S.One,S.Zero,S.Zero])
            rottodirection -= sourcedir * sourcedir.dot(rottodirection)
            M[0:3,0:3] = IKFastSolver.rodrigues(rottodirection.normalized(), atan2(fsin, fcos))
        return M
    
    @staticmethod
    def has(eqs,*sym):
        """check if eqs depends on any variable in sym
        """
        return any([eq.has(*sym) for eq in eqs]) if len(sym) > 0 else False


    def gen_trigsubs(self, trigvars):
        for v in trigvars:
            if v not in self.trigvars_subs and self.IsHinge(v.name):
                self.trigvars_subs.append(v)
                log.info('add %s into self.trigsubs' % v)
                self.trigsubs.append((sin(v)**2, 1-cos(v)**2))
                self.trigsubs.append((Symbol('s%s'%v.name)**2, \
                                      1-Symbol('c%s'%v.name)**2))
        return
    
    def trigsimp_new(self, eq):
        """
        TGN's rewrite version of trigsimp: recursively subs sin**2 for 1-cos**2 for every trig var
        """
        eq = expand(eq)
        curcount = eq.count_ops()
        while True:
            eq = eq.subs(self.trigsubs).expand()
            newcount = eq.count_ops()
            if IKFastSolver.equal(curcount, newcount):
                break
            curcount = newcount
        return eq
    
    """    
    def trigsimp(self, eq, trigvars):

        # recursively subs sin**2 for 1-cos**2 for every trig var

        exec(ipython_str)
        trigsubs = []
        for v in trigvars:
            if self.IsHinge(v.name):
                trigsubs.append((sin(v)**2, 1-cos(v)**2))
                trigsubs.append((Symbol('s%s'%v.name)**2, \
                                 1-Symbol('c%s'%v.name)**2))
                
        eq = expand(eq)
        curcount = eq.count_ops()
        while True:
            eq = eq.subs(trigsubs).expand()
            newcount = eq.count_ops()
            if IKFastSolver.equal(curcount, newcount):
                break
            curcount=newcount
        return eq
    """
    
    def SimplifyAtan2(self, eq, \
                      incos = False, \
                      insin = False, \
                      epsilon = None):
        """
        Simplifies equations involving atan2 and sin/cos/tan

        E.g., sin(atan2(y,x)) <-- y/sqrt(x**2+y**2)
              cos(atan2(y,x)) <-- x/sqrt(x**2+y**2)
              tan(atan2(y,x)) <-- y/x
        
        Sometimes input equations may be like

        sin(-atan2(-r21, -r20))
        cos(-atan2(-r21, -r20) + 3.14159265358979)
        
        Then the internal operations have to be carried over.

        TGN: Can we somehow resolve this problem?
        """
        processed = False
        # incos and insin indicate whether we should take cos/sin into account
        if eq.is_Add:
            if incos:
                # exec(ipython_str)
                lefteq = eq.args[1]
                if len(eq.args) > 2:
                    for ieq in range(2,len(eq.args)):
                        lefteq += eq.args[ieq]
                neweq = \
                        self.SimplifyAtan2(eq.args[0], incos = True) * \
                        self.SimplifyAtan2(lefteq,     incos = True) - \
                        self.SimplifyAtan2(eq.args[0], insin = True) * \
                        self.SimplifyAtan2(lefteq,     insin = True)
                processed = True
                
            elif insin:
                # exec(ipython_str)
                lefteq = eq.args[1]
                if len(eq.args) > 2:
                    for ieq in range(2,len(eq.args)):
                        lefteq += eq.args[ieq]
                neweq = \
                        self.SimplifyAtan2(eq.args[0], incos = True) * \
                        self.SimplifyAtan2(lefteq,     insin = True) + \
                        self.SimplifyAtan2(eq.args[0], insin = True) * \
                        self.SimplifyAtan2(lefteq,     incos = True)
                processed = True
                
            else:
                neweq = S.Zero
                for subeq in eq.args:
                    neweq += self.SimplifyAtan2(subeq)
                # call simplify in order to take in common terms
                if self.codeComplexity(neweq) > 80:
                    neweq2 = neweq
                else:
                    #log.info('complexity: %d', self.codeComplexity(neweq))
                    neweq2 = simplify(neweq)
                if neweq2 != neweq:
                    neweq = self.SimplifyAtan2(neweq2)
                else:
                    try:
                        #print 'simplifying',neweq
                        neweq = self.SimplifyTransform(neweq)
                    except PolynomialError:
                        # ok if neweq is too complicated
                        pass
                    
        elif eq.is_Mul:
            
            if incos and len(eq.args) == 2:
                num = None
                if eq.args[0].is_integer:
                    num = eq.args[0]
                    eq2 = eq.args[1]
                elif eq.args[1].is_integer:
                    num = eq.args[1]
                    eq2 = eq.args[0]
                if num is not None:
                    if num == S.One:
                        neweq = self.SimplifyAtan2(eq2, incos = True)
                        processed = True
                    if num == -S.One:
                        neweq = self.SimplifyAtan2(eq2, incos = True)
                        processed = True
                        
            elif insin and len(eq.args) == 2:
                num = None
                if eq.args[0].is_integer:
                    num = eq.args[0]
                    eq2 = eq.args[1]
                elif eq.args[1].is_integer:
                    num = eq.args[1]
                    eq2 = eq.args[0]
                if num is not None:
                    if num == S.One:
                        neweq = self.SimplifyAtan2(eq2, insin = True)
                        processed = True
                    if num == -S.One:
                        neweq = -self.SimplifyAtan2(eq2, insin = True)
                        processed = True
                        
            if not processed:
                neweq = self.SimplifyAtan2(eq.args[0])
                for subeq in eq.args[1:]:
                    neweq *= self.SimplifyAtan2(subeq)
                    
        elif eq.is_Function:
            
            if incos and eq.func == atan2:
                yeq = self.SimplifyTransform(self.SimplifyAtan2(eq.args[0]))
                xeq = self.SimplifyTransform(self.SimplifyAtan2(eq.args[1]))
                neweq = xeq / sqrt(self.SimplifyTransform(yeq**2+xeq**2))
                processed = True
                
            elif insin and eq.func == atan2:
                yeq = self.SimplifyTransform(self.SimplifyAtan2(eq.args[0]))
                xeq = self.SimplifyTransform(self.SimplifyAtan2(eq.args[1]))
                neweq = yeq / sqrt(self.SimplifyTransform(yeq**2+xeq**2))
                processed = True
                
            elif eq.func == cos:
                neweq = self.SimplifyAtan2(eq.args[0], incos = True)
                
            elif eq.func == sin:
                neweq = self.SimplifyAtan2(eq.args[0], insin = True)
                
            else:
                newargs = [self.SimplifyAtan2(subeq) for subeq in eq.args]
                neweq = eq.func(*newargs)
                
        elif eq.is_Pow:
            neweq = None
            if eq.exp.is_number and eq.exp-0.5 == S.Zero:
                if eq.base.is_Pow and eq.base.exp.is_number and eq.base.exp-2 == S.Zero:
                    # should be abs(eq.base.base), but that could make other simplifications more difficult?
                    neweq = abs(self.SimplifyAtan2(eq.base.base))
                    
            if neweq is None:
                neweq = self.SimplifyAtan2(eq.base)**self.SimplifyAtan2(eq.exp)
                
        elif eq.is_number:            
            if epsilon is None:
                epsilon = 1e-15
            if insin:
                neweq = sin(eq)
            elif incos:
                neweq = cos(eq)
            else:
                neweq = eq
                
            processed = True
            if abs(neweq.evalf()) <= epsilon:
                neweq = S.Zero
                
        else:
            neweq = eq
            
        if not processed and insin:
            return sin(neweq)
        elif not processed and incos:
            return cos(neweq)
        else:
            return neweq

    @staticmethod
    def codeComplexity(expr):
        complexity = 1
        if expr.is_Add or expr.is_Mul:
            complexity += sum(IKFastSolver.codeComplexity(term) for term in expr.args)

        elif expr.is_Function:
            complexity += sum(IKFastSolver.codeComplexity(term) for term in expr.args) + 1
            
        elif expr.is_Pow:
            complexity += IKFastSolver.codeComplexity(expr.base) + \
                          IKFastSolver.codeComplexity(expr.exp)

        elif expr.is_Poly:
            # TGN: this function does not evaluate the complexity of a Poly object???
            # should I add the following?
            # exec(ipython_str) in globals(), locals()
            #
            # complexity += sum(IKFastSolver.codeComplexity(term) for term in expr.args)
            #
            # Not sure if it has the same effect as
            complexity += IKFastSolver.codeComplexity(expr.as_expr())
            
        else: # trivial cases
            assert(expr.is_number or expr.is_Symbol)
            
        return complexity
    
    def ComputePolyComplexity(self, peq):
        """peq is a polynomial
        """
        complexity = 0
        for monoms,coeff in peq.terms():
            coeffcomplexity = self.codeComplexity(coeff)
            for m in monoms:
                if m > 1:
                    complexity += 2
                elif m > 0:
                    complexity += 1
            complexity += coeffcomplexity + 1
        return complexity
    
    def sortComplexity(self, exprs):

        if len(exprs)>2:
            exprs.sort(lambda x, y: \
                       self.codeComplexity(x) - self.codeComplexity(y))
            
        return exprs

    def checkForDivideByZero(self, eq):
        """returns the equations to check for zero
        """
        checkforzeros = []
        try:
            if eq.is_Function:
                if eq.func == atan2:
                    # atan2 is only a problem when both numerator and denominator are 0!
                    #
                    # checkforzeros.append((eq.args[0]**2+eq.args[1]**2).expand())
                    # have to re-substitute given the global symbols
                    #
                    # If args[0] and args[1] are very complicated, then there's no reason to do this check
                    substitutedargs = []
                    for argeq in eq.args:
                        argeq2 = self._SubstituteGlobalSymbols(argeq)
                        if self.codeComplexity(argeq2) < 200:
                            substitutedargs.append(self.SimplifyAtan2(argeq2))
                        else:
                            substitutedargs.append(argeq2)
                    # has to be greater than 20 since some const coefficients can be simplified
                    if self.codeComplexity(substitutedargs[0]) < 30 and self.codeComplexity(substitutedargs[1]) < 30:
                        if (not substitutedargs[0].is_number or substitutedargs[0] == S.Zero) and \
                           (not substitutedargs[1].is_number or substitutedargs[1] == S.Zero):
                            
                                sumeq = substitutedargs[0]**2 + substitutedargs[1]**2
                                if self.codeComplexity(sumeq) < 400:
                                    testeq = self.SimplifyAtan2(sumeq.expand())
                                else:
                                    testeq = sumeq
                                    
                                testeq2 = abs(substitutedargs[0])+abs(substitutedargs[1])
                                if self.codeComplexity(testeq) < self.codeComplexity(testeq2):
                                    testeqmin = testeq
                                else:
                                    testeqmin = testeq2
                                    
                                if testeqmin.is_Mul:
                                    checkforzeros += testeqmin.args
                                else:
                                    checkforzeros.append(testeqmin)
                                    
                                if checkforzeros[-1].evalf() == S.Zero:
                                    raise self.CannotSolveError('equation evaluates to 0, never OK')
                                
                                log.info('add atan2( %r, \n                   %r ) \n' \
                                         + '        check zero ' \
                                         #+ ': %r' \
                                         , substitutedargs[0], substitutedargs[1] \
                                         #, checkforzeros[-1]
                                )
                                
                for arg in eq.args:
                    checkforzeros += self.checkForDivideByZero(arg)
                    
            elif eq.is_Add:
                for arg in eq.args:
                    checkforzeros += self.checkForDivideByZero(arg)
            elif eq.is_Mul:
                for arg in eq.args:
                    checkforzeros += self.checkForDivideByZero(arg)
            elif eq.is_Pow:
                for arg in eq.args:
                    checkforzeros += self.checkForDivideByZero(arg)
                if eq.exp.is_number and eq.exp < 0:
                    checkforzeros.append(eq.base)
        except AssertionError, e:
            log.warn('%s', e)

        if len(checkforzeros) > 0:
            newcheckforzeros = []
            for eqtemp in checkforzeros:
                # check for abs(x**y), in that case choose x
                if eqtemp.is_Function and eqtemp.func == Abs:
                    eqtemp = eqtemp.args[0]
                while eqtemp.is_Pow:
                    eqtemp = eqtemp.base
                #self.codeComplexity(eqtemp)
                if self.codeComplexity(eqtemp) < 500:
                    checkeq = self.removecommonexprs(eqtemp,onlygcd=False,onlynumbers=True)
                    if self.CheckExpressionUnique(newcheckforzeros,checkeq):
                        newcheckforzeros.append(checkeq)
                else:
                    # not even worth checking since the equation is so big...
                    newcheckforzeros.append(eqtemp)
            return newcheckforzeros

        return checkforzeros

    def ComputeSolutionComplexity(self, sol, solvedvars, unsolvedvars):
        """
        For all solutions, check if there is a divide by zero
        
        Fills checkforzeros for the solution
        """
        sol.checkforzeros = sol.getPresetCheckForZeros()
        sol.score = 20000*sol.numsolutions()
        
        try:
            # multiby by 400 in order to prioritize equations with less solutions
            if hasattr(sol,'jointeval') and sol.jointeval is not None:
                for s in sol.jointeval:
                    sol.score += self.codeComplexity(s)
                    sol.checkforzeros += self.checkForDivideByZero(s.subs(sol.dictequations))
                subexprs = sol.jointeval
                
            elif hasattr(sol,'jointevalsin') and sol.jointevalsin is not None:
                for s in sol.jointevalsin:
                    sol.score += self.codeComplexity(s)
                    sol.checkforzeros += self.checkForDivideByZero(s.subs(sol.dictequations))
                subexprs = sol.jointevalsin
                
            elif hasattr(sol,'jointevalcos') and sol.jointevalcos is not None:
                for s in sol.jointevalcos:
                    sol.score += self.codeComplexity(s)
                    sol.checkforzeros += self.checkForDivideByZero(s.subs(sol.dictequations))
                subexprs = sol.jointevalcos
            else:
                return sol.score

            # have to also check solution dictionary
            for s,v in sol.dictequations:
                sol.score += self.codeComplexity(v)
                sol.checkforzeros += self.checkForDivideByZero(v.subs(sol.dictequations))
            
            def checkpow(expr,sexprs):
                score = 0
                if expr.is_Pow:
                    sexprs.append(expr.base)
                    if expr.base.is_finite is not None and not expr.base.is_finite:
                        return oo # infinity
                    if expr.exp.is_number and expr.exp < 0:
                        # check if exprbase contains any variables that have already been solved
                        containsjointvar = expr.base.has(*solvedvars)
                        cancheckexpr = not expr.base.has(*unsolvedvars)
                        score += 10000
                        if not cancheckexpr:
                            score += 100000
                elif not self.isValidSolution(expr):
                    return oo # infinity
                return score
            
            sexprs = subexprs[:]
            while len(sexprs) > 0:
                sexpr = sexprs.pop(0)
                if sexpr.is_Add:
                    sol.score += sum([sum([checkpow(arg2, sexprs) for arg2 in arg.args]) \
                                      if arg.is_Mul else checkpow(arg, sexprs) \
                                      for arg in sexpr.args])
                elif sexpr.is_Mul:
                    sol.score += sum([checkpow(arg,sexprs) for arg in sexpr.args])
                    
                elif sexpr.is_Function:
                    sexprs += sexpr.args
                    
                elif not self.isValidSolution(sexpr):
                    log.warn('not valid: %s', sexpr)
                    sol.score = oo # infinity
                else:
                    sol.score += checkpow(sexpr, sexprs)
                    
        except AssertionError, e:
            log.warn('%s', e)
            sol.score = 1e10

        newcheckforzeros = []
        for eqtemp in sol.checkforzeros:
            if self.codeComplexity(eqtemp) < 1000:
                # if there's a sign, there's an infinite recursion?
                if len(eqtemp.find(sign)) > 0:
                    newcheckforzeros.append(eqtemp)
                else:
                    checkeq = self.removecommonexprs(eqtemp, \
                                                     onlygcd = False, \
                                                     onlynumbers = True)
                    if self.CheckExpressionUnique(newcheckforzeros, checkeq):
                        newcheckforzeros.append(checkeq)
            else:
                newcheckforzeros.append(eqtemp)
        sol.checkforzeros = newcheckforzeros
        return sol.score

    def checkSolvability(self, AllEquations, checkvars, othervars):
        pass

    def checkSolvabilityReal(self, AllEquations, checkvars, othervars):
        """
        Returns true if there are enough equations to solve for checkvars
        """
        subs = []
        checksymbols = []
        allsymbols = []
        for var in checkvars:
            subs += self.Variable(var).subs
            checksymbols += self.Variable(var).vars
        allsymbols = checksymbols[:]
        
        for var in othervars:
            subs += self.Variable(var).subs
            allsymbols += self.Variable(var).vars
            
        found = False
        for testconsistentvalue in self.testconsistentvalues:
            psubvalues = [(s,v) for s,v in testconsistentvalue if not s.has(*checksymbols)]
            eqs = [eq.subs(self.globalsymbols).subs(subs).subs(psubvalues) for eq in AllEquations]
            usedsymbols = [s for s in checksymbols if self.has(eqs,s)]
            eqs = [Poly(eq,*usedsymbols) for eq in eqs if eq != S.Zero]
            # check if any equations have monos of degree more than 1, if yes, then quit with success since 0.6.7 sympy solver will freeze
            numhigherpowers = 0
            for eq in eqs:
                for monom in eq.monoms():
                    if any([m > 1 for m in monom]):
                        numhigherpowers += 1
            if numhigherpowers > 0:
                log.info('checkSolvability has %d higher powers, returning solvable if > 6'%numhigherpowers)
                if numhigherpowers > 6:
                    found = True
                    break
            for var in checkvars:
                varsym = self.Variable(var)
                if self.IsHinge(var.name):
                    if varsym.cvar in usedsymbols and varsym.svar in usedsymbols:
                        eqs.append(Poly(varsym.cvar**2+varsym.svar**2-1,*usedsymbols))
            # have to make sure there are representative symbols of all the checkvars, otherwise degenerate solution
            setusedsymbols = set(usedsymbols)
            if any([len(setusedsymbols.intersection(self.Variable(var).vars)) == 0 for var in checkvars]):
                continue
            
            try:
                sol = solve_poly_system(eqs)
                if sol is not None and len(sol) > 0 and len(sol[0]) == len(usedsymbols):
                    found = True
                    break
            except:
                pass
            
        if not found:
            raise self.IKFeasibilityError(AllEquations,checkvars)
        
    def writeIkSolver(self, chaintree, lang = None):
        """
        Write the AST into C++
        """
        self._CheckPreemptFn(progress = 0.5)
        if lang is None:
            if CodeGenerators.has_key('cpp'):
                lang = 'cpp'
            else:
                lang = CodeGenerators.keys()[0]
                
        log.info('generating %s code...'%lang)
        
        if self._checkpreemptfn is not None:
            import weakref
            weakself = weakref.proxy(self)
            
            def _CheckPreemtCodeGen(msg, progress):
                # put the progress in the latter half
                weakself._checkpreemptfn(u'CodeGen %s'%msg, 0.5+0.5*progress)
                
        else:
            _CheckPreemtCodeGen = None
            
        return CodeGenerators[lang](kinematicshash = self.kinematicshash, \
                                    version = __version__, \
                                    iktypestr = self._iktype, \
                                    checkpreemptfn = _CheckPreemtCodeGen)\
                                    .generate(chaintree)
    
    def generateIkSolver(self, baselink, eelink, \
                         freeindices = None, \
                         solvefn = None, \
                         ikfastoptions = 0):
        """
        :param ikfastoptions: options that control how ikfast.
        """
        self._CheckPreemptFn(progress = 0)
        
        if solvefn is None:
            solvefn = IKFastSolver.solveFullIK_6D
            
        chainlinks  = self.kinbody.GetChain(baselink,eelink,returnjoints = False)
        chainjoints = self.kinbody.GetChain(baselink,eelink,returnjoints =  True)
        LinksRaw, jointvars = self.forwardKinematicsChain(chainlinks,chainjoints)
        
        for T in LinksRaw:
            log.info('[' + ','.join(['[%s, %s, %s, %s]' % \
                                     (T[i,0], T[i,1], T[i,2], T[i,3]) \
                                     for i in range(3)]) + ']')
            
        self.degeneratecases = None
        
        if freeindices is None:
            # need to iterate through all combinations of free joints
            assert(0)
            
        isolvejointvars = []
        solvejointvars = []
        self._ikfastoptions = ikfastoptions
        self.ifreejointvars = []
        self.freevarsubs = []
        self.freevarsubsinv = []
        self.freevars = []
        self.freejointvars = []
        self.invsubs = []
        for i,v in enumerate(jointvars):
            var = self.Variable(v)      # call IKFastSolver.Variable constructor
            axis = self.axismap[v.name] # axismap is dictionary
            dofindex = axis.joint.GetDOFIndex()+axis.iaxis
            if dofindex in freeindices:
                # convert all free variables to constants
                self.ifreejointvars.append(i)
                self.freevarsubs += [(cos(var.var), var.cvar), \
                                     (sin(var.var), var.svar)]
                self.freevarsubsinv += [(var.cvar,cos(var.var)), \
                                        (var.svar,sin(var.var))]
                self.freevars += [var.cvar,var.svar]
                self.freejointvars.append(var.var)
            else:
                solvejointvars.append(v)
                isolvejointvars.append(i)
                self.invsubs += [(var.cvar,cos(v)),\
                                 (var.svar,sin(v))]

        self._solvejointvars = solvejointvars
        self._jointvars = jointvars

        """
        set up for the end-effector 
        (1) symbols, by using Symbol("..."), and
        (2) symbolic variables, by assigning them their corresponding symbols
        """
        # rotation matrix R
        self.Tee = eye(4)
        for i in range(0,3):
            for j in range(0,3):
                self.Tee[i,j] = Symbol("r%d%d"%(i,j))

        # coordinate vector p
        self.Tee[0,3] = Symbol("px")
        self.Tee[1,3] = Symbol("py")
        self.Tee[2,3] = Symbol("pz")

        # symbolic variables
        r00,r01,r02,px,r10,r11,r12,py,r20,r21,r22,pz = self.Tee[0:12]
        self.pp = Symbol('pp')
        self.ppsubs = [(self.pp, \
                        px**2+py**2+pz**2)]

        # dot product of p and each column of R
        self.npxyz = [Symbol('npx'),Symbol('npy'),Symbol('npz')]
        self.npxyzsubs = [(self.npxyz[i], \
                           px*self.Tee[0,i]+py*self.Tee[1,i]+pz*self.Tee[2,i]) for i in range(3)]
        
        # cross products between columns of R
        self.rxp = []
        self.rxpsubs = []
        for i in range(3):
            self.rxp.append([Symbol('rxp%d_%d'%(i,j)) for j in range(3)])
            c = self.Tee[0:3,i].cross(self.Tee[0:3,3])
            self.rxpsubs += [(self.rxp[-1][j],c[j]) for j in range(3)]

        # have to include new_rXX
        self.pvars = self.Tee[0:12] + \
                     self.npxyz+[self.pp] + self.rxp[0]+self.rxp[1]+self.rxp[2] + \
                     [Symbol('new_r00'), Symbol('new_r01'), Symbol('new_r02'), \
                      Symbol('new_r10'), Symbol('new_r11'), Symbol('new_r12'), \
                      Symbol('new_r20'), Symbol('new_r21'), Symbol('new_r22')]
        self._rotsymbols = list(self.Tee[0:3,0:3])

        # add positions
        ip = 9
        inp = 12
        ipp = 15
        irxp = 16
        self._rotpossymbols = self._rotsymbols + \
                              list(self.Tee[0:3,3])+self.npxyz+[self.pp]+self.rxp[0]+self.rxp[1]+self.rxp[2]

        # 2-norm of each row/column vector in R is 1
        # groups of rotation variables are unit vectors
        self._rotnormgroups = []
        for i in range(3):
            # row
            self._rotnormgroups.append([self.Tee[i,0], self.Tee[i,1], self.Tee[i,2], S.One])
            # column
            self._rotnormgroups.append([self.Tee[0,i], self.Tee[1,i], self.Tee[2,i], S.One])
            
        self._rotposnormgroups = list(self._rotnormgroups)
        self._rotposnormgroups.append([self.Tee[0,3], self.Tee[1,3], self.Tee[2,3], self.pp])
        
        # dot product of each pair of rows/columns in R are 0
        self._rotdotgroups = []
        for i,j in [(0,1),(0,2),(1,2)]: #combinations(range(3),2):
            # pair of rows
            self._rotdotgroups.append([[3*i,3*j], [3*i+1,3*j+1], [3*i+2,3*j+2], S.Zero])
            # pair of columns
            self._rotdotgroups.append([[i,j], [i+3,j+3], [i+6,j+6], S.Zero])

        self._rotposdotgroups = list(self._rotdotgroups)
        for i in range(3):
            self._rotposdotgroups.append([[i,ip], [i+3,ip+1], [i+6,ip+2], self.npxyz[i]])
            self._rotposdotgroups.append([[3*i+0,inp], [3*i+1,inp+1], [3*i+2,inp+2], self.Tee[i,3]])
        self._rotcrossgroups = []

        """

Numbering of entries in A and inv(A):

         [ r00  r01  r02   px ]    [ 0  1  2   9 ]
         [ r10  r11  r12   py ]    [ 3  4  5  10 ]
     A = [ r20  r21  r22   pz ]    [ 6  7  8  11 ]
         [                  1 ]    

         [ r00  r10  r20  npx ]    [ 0  3  6  12 ]
         [ r01  r11  r21  npy ]    [ 1  4  7  13 ]
inv(A) = [ r02  r12  r22  npz ]    [ 2  5  8  14 ]
         [                  1 ]    


[[[0, 3], [1, 4], [2, 5], 0],
 [[0, 1], [3, 4], [6, 7], 0],
 [[0, 6], [1, 7], [2, 8], 0],
 [[0, 2], [3, 5], [6, 8], 0],
 [[3, 6], [4, 7], [5, 8], 0],
 [[1, 2], [4, 5], [7, 8], 0],
------------------------- Above are _rotdotgroups
------------------------- Below are _rotposdotgroups
 [[0, 9], [3, 10], [6, 11], npx],
 [[0, 12], [1, 13], [2, 14], px],
 [[1, 9], [4, 10], [7, 11], npy],
 [[3, 12], [4, 13], [5, 14], py],
 [[2, 9], [5, 10], [8, 11], npz],
 [[6, 12], [7, 13], [8, 14], pz]]

        """
        
        # cross product of each pair of rows/columns is the remaining row/column
        for i,j,k in [(0,1,2),(1,2,0),(0,2,1)]:
            # pair of columns
            self._rotcrossgroups.append([[i+3,j+6], [i+6,j+3],k  ])
            self._rotcrossgroups.append([[i+6,j],   [i,j+6],  k+3])
            self._rotcrossgroups.append([[i,  j+3], [i+3,j],  k+6])
            # pair of rows
            self._rotcrossgroups.append([[3*i+1,3*j+2], [3*i+2,3*j+1], 3*k  ])
            self._rotcrossgroups.append([[3*i+2,3*j],   [3*i,3*j+2],   3*k+1])
            self._rotcrossgroups.append([[3*i,  3*j+1], [3*i+1,3*j],   3*k+2])
            # swap if sign is negative: if j!=1+i
            # i.e. k==1, the 2nd row/column; will change into
            # if k==1:
            if j!=1+i:
                assert(k==1)
                for crossgroup in self._rotcrossgroups[-6:]:
                    crossgroup[0],crossgroup[1] = crossgroup[1], crossgroup[0]

        # add positions
        self._rotposcrossgroups = list(self._rotcrossgroups)
        for i in range(3):
            # column i cross position
            self._rotposcrossgroups.append([[i+3,ip+2], [i+6,ip+1], irxp+3*i+0])
            self._rotposcrossgroups.append([[i+6,ip+0], [i,  ip+2], irxp+3*i+1])
            self._rotposcrossgroups.append([[i,  ip+1], [i+3,ip+0], irxp+3*i+2])
            
        """ TGN: what are _rotposcrossgroups?
[[[3, 7], [6, 4], 2],
 [[6, 1], [0, 7], 5],
 [[0, 4], [3, 1], 8],
 [[1, 5], [2, 4], 6],
 [[2, 3], [0, 5], 7],
 [[0, 4], [1, 3], 8],
 [[4, 8], [7, 5], 0],
 [[7, 2], [1, 8], 3],
 [[1, 5], [4, 2], 6],
 [[4, 8], [5, 7], 0],
 [[5, 6], [3, 8], 1],
 [[3, 7], [4, 6], 2],
 [[6, 5], [3, 8], 1],----
 [[0, 8], [6, 2], 4],    \
 [[3, 2], [0, 5], 7],     \ [0] and [1] are swapped when k==1
 [[2, 7], [1, 8], 3],     /
 [[0, 8], [2, 6], 4],    /
 [[1, 6], [0, 7], 5],----
------------------------- Above are _rotcrossgroups
------------------------- Below are _rotposcrossgroups
                          16--24 are what positions?
 [[3, 11], [6, 10], 16],
 [[6, 9], [0, 11], 17],
 [[0, 10], [3, 9], 18],
 [[4, 11], [7, 10], 19],
 [[7, 9], [1, 11], 20],
 [[1, 10], [4, 9], 21],
 [[5, 11], [8, 10], 22],
 [[8, 9], [2, 11], 23],
 [[2, 10], [5, 9], 24]]
        """
            
        self.Teeinv = self.affineInverse(self.Tee)

        LinksLeft = []
        if self.useleftmultiply:
            while not self.has(LinksRaw[0], *solvejointvars):
                LinksLeft.append(LinksRaw.pop(0))
                LinksLeftInv = [self.affineInverse(T) for T in LinksLeft]
        self.testconsistentvalues = None

        self.gsymbolgen = cse_main.numbered_symbols('gconst')
        self.globalsymbols = []
        self._scopecounter = 0

# before passing to the solver, set big numbers to constant variables, this will greatly reduce computation times
#         numbersubs = []
#         LinksRaw2 = []
#         for Torig in LinksRaw:
#             T = Matrix(Torig)
#             #print axisAngleFromRotationMatrix(numpy.array(numpy.array(T[0:3,0:3]),numpy.float64))
#             for i in range(12):
#                 ti = T[i]
#                 if ti.is_number and len(str(ti)) > 30:
#                     matchnumber = self.MatchSimilarFraction(ti,numbersubs)
#                     if matchnumber is None:
#                         sym = self.gsymbolgen.next()
#                         log.info('adding global symbol %s=%s'%(sym,ti))
#                         numbersubs.append((sym,ti))
#                         T[i] = sym
#                     else:
#                         T[i] = matchnumber
#             LinksRaw2.append(T)
#         if len(numbersubs) > 10:
#             log.info('substituting %d global symbols',len(numbersubs))
#             LinksRaw = LinksRaw2
#             self.globalsymbols += numbersubs

        self.Teeleftmult = self.multiplyMatrix(LinksLeft) # the raw ee passed to the ik solver function
        self._CheckPreemptFn(progress=0.01)

        print('========================= START OF SETUP PRINT ===============================\n')
        info_to_print =  ['ifreejointvars',
                          'freevarsubs',
                          'freevarsubsinv',
                          'freevars',
                          'freejointvars', 
                          'invsubs',
                          '_solvejointvars',
                          '_jointvars',
                          'Tee',
                          'pp',
                          'ppsubs',
                          'npxyz',
                          'npxyzsubs',
                          'rxp',
                          'rxpsubs',
                          'pvars',
                          '_rotsymbols',
                          '_rotpossymbols',
                          '_rotnormgroups',
                          '_rotposnormgroups',
                          '_rotdotgroups',
                          '_rotposdotgroups',
                          '_rotcrossgroups',
                          '_rotposcrossgroups',
                          'Teeinv'
                          ]
        for each_info in info_to_print:
            print('\n%s' % each_info)
            exec_str = "print \"      \", self." + each_info
            exec(exec_str)
        print('\n')
        print('========================== END OF SETUP PRINT ================================\n')
        
        # MAIN FUNCTION
        self.gen_trigsubs(jointvars)
        chaintree = solvefn(self, LinksRaw, jointvars, isolvejointvars)
        if self.useleftmultiply:
            chaintree.leftmultiply(Tleft=self.multiplyMatrix(LinksLeft), Tleftinv=self.multiplyMatrix(LinksLeftInv[::-1]))
        chaintree.dictequations += self.globalsymbols
        return chaintree

    def MatchSimilarFraction(self,num,numbersubs,matchlimit = 40):
        """returns None if no appropriate match found
        """
        for c,v in numbersubs:
            if self.equal(v,num):
                return c
        
        # nothing matched, so check gcd
        largestgcd = S.One
        retnum = None
        for c,v in numbersubs:
            curgcd = gcd(v,num)
            if len(str(curgcd)) > len(str(largestgcd)):
                newfraction = (num/v)
                if len(str(newfraction)) <= matchlimit:
                    largestgcd = curgcd
                    retnum = c * newfraction
        return retnum

    def ComputeConsistentValues(self,jointvars,T,numsolutions=1,subs=None):
        """computes a set of substitutions that satisfy the IK equations 
        """
        possibleangles_old = [S.Zero, pi.evalf()/2, asin(3.0/5).evalf(), asin(4.0/5).evalf(), asin(5.0/13).evalf(), asin(12.0/13).evalf()]
        possibleangles = [self.convertRealToRational(x) for x in possibleangles_old]
        # TGN: use symbolic numbers for all possible angles instead of floating-point numbers
        
        possibleanglescos = [S.One, S.Zero, Rational(4,5), Rational(3,5), Rational(12,13), Rational(5,13)]
        possibleanglessin = [S.Zero, S.One, Rational(3,5), Rational(4,5), Rational(5,13), Rational(12,13)]
        testconsistentvalues = []
        varsubs = []
        for jointvar in jointvars:
            varsubs += self.Variable(jointvar).subs
            
        for isol in range(numsolutions):

            inds = [0]*len(jointvars)
            if isol < numsolutions-1:
                for j in range(len(jointvars)):
                    inds[j] = (isol+j)%len(possibleangles)
                    
            valsubs = []
            for i,ind in enumerate(inds):
                v,s,c = possibleangles[ind],possibleanglessin[ind],possibleanglescos[ind]
                var = self.Variable(jointvars[i])
                valsubs += [(var.var,v),(var.cvar,c),(var.svar,s),(var.tvar,s/c),(var.htvar,s/(1+c))]
                
            psubs = []
            for i in range(12):
                psubs.append((self.pvars[i],T[i].subs(varsubs).subs(self.globalsymbols+valsubs)))
            for s,v in self.ppsubs+self.npxyzsubs+self.rxpsubs:
                psubs.append((s,v.subs(psubs)))
                
            allsubs = valsubs+psubs
            if subs is not None:
                allsubs += [(dvar,var.subs(varsubs).subs(valsubs)) for dvar,var in subs]
            testconsistentvalues.append(allsubs)


        print('========================== START OF CONSISTENT VALUES PRINT ================================\n')
        set_num_counter = 0
        for each_set_consistent_values in testconsistentvalues:
            item_counter = 0
            print 'Set ', set_num_counter
            print '------------------------------------------'
            for val in each_set_consistent_values:
                print val[0], "=", val[1], ",",
                item_counter += 1
                if item_counter in [5,10,15,20,25,30,35,39,43,47,51,54,57]:
                    print('')
            print('\n')
            set_num_counter += 1
        print('========================== END OF CONSISTENT VALUES PRINT  ================================\n')
        return testconsistentvalues

    def solveFullIK_Direction3D(self,LinksRaw, jointvars, isolvejointvars, rawmanipdir=Matrix(3,1,[S.Zero,S.Zero,S.One])):
        """manipdir needs to be filled with a 3elemtn vector of the initial direction to control"""
        self._iktype = 'direction3d'
        manipdir = Matrix(3,1,[Float(x,30) for x in rawmanipdir])
        manipdir /= sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2])
        for i in range(3):
            manipdir[i] = self.convertRealToRational(manipdir[i])
        Links = LinksRaw[:]
        LinksInv = [self.affineInverse(link) for link in Links]
        T = self.multiplyMatrix(Links)
        self.Tfinal = zeros((4,4))
        self.Tfinal[0,0:3] = (T[0:3,0:3]*manipdir).transpose()
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        endbranchtree = [AST.SolverStoreSolution(jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 2:
            raise self.CannotSolveError('need 2 joints')

        log.info('ikfast direction3d: %s',solvejointvars)

        Daccum = self.Tee[0,0:3].transpose()
        numvarsdone = 2
        Ds = []
        Dsee = []
        for i in range(len(Links)-1):
            T = self.multiplyMatrix(Links[i:])
            D = T[0:3,0:3]*manipdir
            hasvars = [self.has(D,v) for v in solvejointvars]
            if __builtin__.sum(hasvars) == numvarsdone:
                Ds.append(D)
                Dsee.append(Daccum)
                numvarsdone -= 1
            Tinv = self.affineInverse(Links[i])
            Daccum = Tinv[0:3,0:3]*Daccum
        AllEquations = self.buildEquationsFromTwoSides(Ds,Dsee,jointvars,uselength=False)
        self.checkSolvability(AllEquations,solvejointvars,self.freejointvars)
        tree = self.SolveAllEquations(AllEquations,curvars=solvejointvars,othersolvedvars = self.freejointvars[:],solsubs = self.freevarsubs[:],endbranchtree=endbranchtree)
        tree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,tree)
        return AST.SolverIKChainDirection3D([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], Dee=self.Tee[0,0:3].transpose().subs(self.freevarsubs), jointtree=tree,Dfk=self.Tfinal[0,0:3].transpose())

    def solveFullIK_Lookat3D(self,LinksRaw, jointvars, isolvejointvars,rawmanipdir=Matrix(3,1,[S.Zero,S.Zero,S.One]),rawmanippos=Matrix(3,1,[S.Zero,S.Zero,S.Zero])):
        """manipdir,manippos needs to be filled with a direction and position of the ray to control the lookat
        """
        self._iktype = 'lookat3d'
        manipdir = Matrix(3,1,[Float(x,30) for x in rawmanipdir])
        manippos = Matrix(3,1,[self.convertRealToRational(x) for x in rawmanippos])
        manipdir /= sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2])
        for i in range(3):
            manipdir[i] = self.convertRealToRational(manipdir[i])
        manippos = manippos-manipdir*manipdir.dot(manippos)
        Links = LinksRaw[:]
        LinksInv = [self.affineInverse(link) for link in Links]
        T = self.multiplyMatrix(Links)
        self.Tfinal = zeros((4,4))
        self.Tfinal[0,0:3] = (T[0:3,0:3]*manipdir).transpose()
        self.Tfinal[0:3,3] = T[0:3,0:3]*manippos+T[0:3,3]
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 2:
            raise self.CannotSolveError('need 2 joints')

        log.info('ikfast lookat3d: %s',solvejointvars)
        
        Paccum = self.Tee[0:3,3]
        numvarsdone = 2
        Positions = []
        Positionsee = []
        for i in range(len(Links)-1):
            T = self.multiplyMatrix(Links[i:])
            P = T[0:3,0:3]*manippos+T[0:3,3]
            D = T[0:3,0:3]*manipdir
            hasvars = [self.has(P,v) or self.has(D,v) for v in solvejointvars]
            if __builtin__.sum(hasvars) == numvarsdone:
                Positions.append(P.cross(D))
                Positionsee.append(Paccum.cross(D))
                numvarsdone -= 1
            Tinv = self.affineInverse(Links[i])
            Paccum = Tinv[0:3,0:3]*Paccum+Tinv[0:3,3]

        frontcond = (Links[-1][0:3,0:3]*manipdir).dot(Paccum-(Links[-1][0:3,0:3]*manippos+Links[-1][0:3,3]))
        for v in jointvars:
            frontcond = frontcond.subs(self.Variable(v).subs)
        endbranchtree = [AST.SolverStoreSolution (jointvars,checkgreaterzero=[frontcond],isHinge=[self.IsHinge(var.name) for var in jointvars])]
        AllEquations = self.buildEquationsFromTwoSides(Positions,Positionsee,jointvars,uselength=True)
        self.checkSolvability(AllEquations,solvejointvars,self.freejointvars)
        tree = self.SolveAllEquations(AllEquations,curvars=solvejointvars,othersolvedvars = self.freejointvars[:],solsubs = self.freevarsubs[:],endbranchtree=endbranchtree)
        tree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,tree)
        chaintree = AST.SolverIKChainLookat3D([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], Pee=self.Tee[0:3,3].subs(self.freevarsubs), jointtree=tree,Dfk=self.Tfinal[0,0:3].transpose(),Pfk=self.Tfinal[0:3,3])
        chaintree.dictequations += self.ppsubs
        return chaintree

    def solveFullIK_Rotation3D(self,LinksRaw, jointvars, isolvejointvars, Rbaseraw=eye(3)):
        self._iktype = 'rotation3d'
        Rbase = eye(4)
        for i in range(3):
            for j in range(3):
                Rbase[i,j] = self.convertRealToRational(Rbaseraw[i,j])
        Tfirstright = LinksRaw[-1]*Rbase
        Links = LinksRaw[:-1]
        LinksInv = [self.affineInverse(link) for link in Links]
        self.Tfinal = self.multiplyMatrix(Links)
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 3:
            raise self.CannotSolveError('need 3 joints')
        
        log.info('ikfast rotation3d: %s',solvejointvars)

        AllEquations = self.buildEquationsFromRotation(Links,self.Tee[0:3,0:3],solvejointvars,self.freejointvars)
        self.checkSolvability(AllEquations,solvejointvars,self.freejointvars)
        tree = self.SolveAllEquations(AllEquations,curvars=solvejointvars[:],othersolvedvars=self.freejointvars,solsubs = self.freevarsubs[:],endbranchtree=endbranchtree)
        tree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,tree)
        return AST.SolverIKChainRotation3D([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], (self.Tee[0:3,0:3] * self.affineInverse(Tfirstright)[0:3,0:3]).subs(self.freevarsubs), tree, Rfk = self.Tfinal[0:3,0:3] * Tfirstright[0:3,0:3])

    def solveFullIK_TranslationLocalGlobal6D(self,LinksRaw, jointvars, isolvejointvars, Tmanipraw=eye(4)):
        self._iktype = 'translation3d'
        Tgripper = eye(4)
        for i in range(4):
            for j in range(4):
                Tgripper[i,j] = self.convertRealToRational(Tmanipraw[i,j])
        localpos = Matrix(3,1,[self.Tee[0,0],self.Tee[1,1],self.Tee[2,2]])
        chain = self._solveFullIK_Translation3D(LinksRaw,jointvars,isolvejointvars,Tgripper[0:3,3]+Tgripper[0:3,0:3]*localpos,False)
        chain.uselocaltrans = True
        return chain
    def solveFullIK_Translation3D(self,LinksRaw, jointvars, isolvejointvars, rawmanippos=Matrix(3,1,[S.Zero,S.Zero,S.Zero])):
        self._iktype = 'translation3d'
        manippos = Matrix(3,1,[self.convertRealToRational(x) for x in rawmanippos])
        return self._solveFullIK_Translation3D(LinksRaw,jointvars,isolvejointvars,manippos)
    
    def _solveFullIK_Translation3D(self,LinksRaw, jointvars, isolvejointvars, manippos,check=True):
        Links = LinksRaw[:]
        LinksInv = [self.affineInverse(link) for link in Links]
        self.Tfinal = self.multiplyMatrix(Links)
        self.Tfinal[0:3,3] = self.Tfinal[0:3,0:3]*manippos+self.Tfinal[0:3,3]
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 3:
            raise self.CannotSolveError('need 3 joints')
        
        log.info('ikfast translation3d: %s',solvejointvars)
        Tmanipposinv = eye(4)
        Tmanipposinv[0:3,3] = -manippos
        T1links = [Tmanipposinv]+LinksInv[::-1]+[self.Tee]
        T1linksinv = [self.affineInverse(Tmanipposinv)]+Links[::-1]+[self.Teeinv]
        AllEquations = self.buildEquationsFromPositions(T1links,T1linksinv,solvejointvars,self.freejointvars,uselength=True)
        if check:
            self.checkSolvability(AllEquations,solvejointvars,self.freejointvars)
        transtree = self.SolveAllEquations(AllEquations,curvars=solvejointvars[:],othersolvedvars=self.freejointvars,solsubs = self.freevarsubs[:],endbranchtree=endbranchtree)
        transtree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,transtree)
        chaintree = AST.SolverIKChainTranslation3D([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], Pee=self.Tee[0:3,3], jointtree=transtree, Pfk = self.Tfinal[0:3,3])
        chaintree.dictequations += self.ppsubs
        return chaintree

    def solveFullIK_TranslationXY2D(self,LinksRaw, jointvars, isolvejointvars, rawmanippos=Matrix(2,1,[S.Zero,S.Zero])):
        self._iktype = 'translationxy2d'
        self.ppsubs = [] # disable since pz is not valid
        self.pp = None
        manippos = Matrix(2,1,[self.convertRealToRational(x) for x in rawmanippos])
        Links = LinksRaw[:]
        LinksInv = [self.affineInverse(link) for link in Links]
        self.Tfinal = self.multiplyMatrix(Links)
        self.Tfinal[0:2,3] = self.Tfinal[0:2,0:2]*manippos+self.Tfinal[0:2,3]
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 2:
            raise self.CannotSolveError('need 2 joints')

        log.info('ikfast translationxy2d: %s',solvejointvars)
        Tmanipposinv = eye(4)
        Tmanipposinv[2,2] = S.Zero
        Tmanipposinv[0:2,3] = -manippos
        Tmanippos = eye(4)
        Tmanippos[2,2] = S.Zero
        Tmanippos[0:2,3] = manippos
        T1links = [Tmanipposinv]+LinksInv[::-1]+[self.Tee]
        T1linksinv = [Tmanippos]+Links[::-1]+[self.Teeinv]
        Taccum = eye(4)
        numvarsdone = 1
        Positions = []
        Positionsee = []
        for i in range(len(T1links)-1):
            Taccum = T1linksinv[i]*Taccum
            hasvars = [self.has(Taccum,v) for v in solvejointvars]
            if __builtin__.sum(hasvars) == numvarsdone:
                Positions.append(Taccum[0:2,3])
                Positionsee.append(self.multiplyMatrix(T1links[(i+1):])[0:2,3])
                numvarsdone += 1
            if numvarsdone > 2:
                # more than 2 variables is almost always useless
                break
        if len(Positions) == 0:
            Positions.append(zeros((2,1)))
            Positionsee.append(self.multiplyMatrix(T1links)[0:2,3])
        AllEquations = self.buildEquationsFromTwoSides(Positions,Positionsee,solvejointvars+self.freejointvars,uselength=True)

        self.checkSolvability(AllEquations,solvejointvars,self.freejointvars)
        transtree = self.SolveAllEquations(AllEquations,curvars=solvejointvars[:],othersolvedvars=self.freejointvars,solsubs = self.freevarsubs[:],endbranchtree=endbranchtree)
        transtree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,transtree)
        chaintree = AST.SolverIKChainTranslationXY2D([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], Pee=self.Tee[0:2,3], jointtree=transtree, Pfk = self.Tfinal[0:2,3])
        chaintree.dictequations += self.ppsubs
        return chaintree

    def solveFullIK_TranslationXYOrientation3D(self,LinksRaw, jointvars, isolvejointvars, rawmanippos=Matrix(2,1,[S.Zero,S.Zero]), rawangle=S.Zero):
        self._iktype = 'translationxyorientation3d'
        raise self.CannotSolveError('TranslationXYOrientation3D not implemented yet')

    def solveFullIK_Ray4D(self,LinksRaw, jointvars, isolvejointvars, rawmanipdir=Matrix(3,1,[S.Zero,S.Zero,S.One]),rawmanippos=Matrix(3,1,[S.Zero,S.Zero,S.Zero])):
        """manipdir,manippos needs to be filled with a direction and position of the ray to control"""
        self._iktype = 'ray4d'
        manipdir = Matrix(3,1,[Float(x,30) for x in rawmanipdir])
        manippos = Matrix(3,1,[self.convertRealToRational(x) for x in rawmanippos])
        manipdir /= sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2])
        for i in range(3):
            manipdir[i] = self.convertRealToRational(manipdir[i])
        manippos = manippos-manipdir*manipdir.dot(manippos)
        Links = LinksRaw[:]
        LinksInv = [self.affineInverse(link) for link in Links]
        T = self.multiplyMatrix(Links)
        self.Tfinal = zeros((4,4))
        self.Tfinal[0,0:3] = (T[0:3,0:3]*manipdir).transpose()
        self.Tfinal[0:3,3] = T[0:3,0:3]*manippos+T[0:3,3]
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 4:
            raise self.CannotSolveError('need 4 joints')

        log.info('ikfast ray4d: %s',solvejointvars)
        
        Pee = self.Tee[0:3,3]
        Dee = self.Tee[0,0:3].transpose()
        numvarsdone = 2
        Positions = []
        Positionsee = []
        for i in range(len(Links)-1):
            T = self.multiplyMatrix(Links[i:])
            P = T[0:3,0:3]*manippos+T[0:3,3]
            D = T[0:3,0:3]*manipdir
            hasvars = [self.has(P,v) or self.has(D,v) for v in solvejointvars]
            if __builtin__.sum(hasvars) == numvarsdone:
                Positions.append(P.cross(D))
                Positionsee.append(Pee.cross(Dee))
                Positions.append(D)
                Positionsee.append(Dee)
                break
            Tinv = self.affineInverse(Links[i])
            Pee = Tinv[0:3,0:3]*Pee+Tinv[0:3,3]
            Dee = Tinv[0:3,0:3]*Dee
        AllEquations = self.buildEquationsFromTwoSides(Positions,Positionsee,jointvars,uselength=True)
        self.checkSolvability(AllEquations,solvejointvars,self.freejointvars)

        #try:
        tree = self.SolveAllEquations(AllEquations,curvars=solvejointvars[:],othersolvedvars = self.freejointvars[:],solsubs = self.freevarsubs[:],endbranchtree=endbranchtree)
        #except self.CannotSolveError:
            # build the raghavan/roth equations and solve with higher power methods
        #    pass
        tree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,tree)
        chaintree = AST.SolverIKChainRay([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], Pee=self.Tee[0:3,3].subs(self.freevarsubs), Dee=self.Tee[0,0:3].transpose().subs(self.freevarsubs),jointtree=tree,Dfk=self.Tfinal[0,0:3].transpose(),Pfk=self.Tfinal[0:3,3])
        chaintree.dictequations += self.ppsubs
        return chaintree
    
    def solveFullIK_TranslationDirection5D(self, LinksRaw, jointvars, isolvejointvars, rawmanipdir=Matrix(3,1,[S.Zero,S.Zero,S.One]),rawmanippos=Matrix(3,1,[S.Zero,S.Zero,S.Zero])):
        """Solves 3D translation + 3D direction
        """
        self._iktype = 'translationdirection5d'
        manippos = Matrix(3,1,[self.convertRealToRational(x) for x in rawmanippos])
        manipdir = Matrix(3,1,[Float(x,30) for x in rawmanipdir])
        manipdir /= sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2])
        # try to simplify manipdir based on possible angles
        for i in range(3):
            value = None
            # TODO should restore 12 once we can capture stuff like pi/12+sqrt(12531342/5141414)
            for num in [3,4,5,6,7,8]:#,12]:
                if abs((manipdir[i]-cos(pi/num))).evalf() <= (10**-self.precision):
                    value = cos(pi/num)
                    break
                elif abs((manipdir[i]+cos(pi/num))).evalf() <= (10**-self.precision):
                    value = -cos(pi/num)
                    break
                elif abs((manipdir[i]-sin(pi/num))).evalf() <= (10**-self.precision):
                    value = sin(pi/num)
                    break
                elif abs((manipdir[i]+sin(pi/num))).evalf() <= (10**-self.precision):
                    value = -sin(pi/num)
                    break
            if value is not None:
                manipdir[i] = value
            else:
                manipdir[i] = self.convertRealToRational(manipdir[i],5)
        manipdirlen2 = trigsimp(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2]) # unfortunately have to do it again...
        manipdir /= sqrt(manipdirlen2)
        
        offsetdist = manipdir.dot(manippos)
        manippos = manippos-manipdir*offsetdist
        Links = LinksRaw[:]
        
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        numzeros = int(manipdir[0]==S.Zero) + int(manipdir[1]==S.Zero) + int(manipdir[2]==S.Zero)
#         if numzeros < 2:
#             try:
#                 log.info('try to rotate the last joint so that numzeros increases')
#                 assert(not self.has(Links[-1],*solvejointvars))
#                 localdir = Links[-1][0:3,0:3]*manipdir
#                 localpos = Links[-1][0:3,0:3]*manippos+Links[-1][0:3,3]
#                 AllEquations = Links[-2][0:3,0:3]*localdir
#                 tree=self.SolveAllEquations(AllEquations,curvars=solvejointvars[-1:],othersolvedvars = [],solsubs = [],endbranchtree=[])
#                 offset = tree[0].jointeval[0]
#                 endbranchtree[0].offsetvalues = [S.Zero]*len(solvejointvars)
#                 endbranchtree[0].offsetvalues[-1] = offset
#                 Toffset = Links[-2].subs(solvejointvars[-1],offset).evalf()
#                 localdir2 = Toffset[0:3,0:3]*localdir
#                 localpos2 = Toffset[0:3,0:3]*localpos+Toffset[0:3,3]
#                 Links[-1]=eye(4)
#                 for i in range(3):
#                     manipdir[i] = self.convertRealToRational(localdir2[i])
#                 manipdir /= sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2]) # unfortunately have to do it again...
#                 manippos = Matrix(3,1,[self.convertRealToRational(x) for x in localpos2])
#             except Exception, e:
#                 print 'failed to rotate joint correctly',e

        LinksInv = [self.affineInverse(link) for link in Links]
        T = self.multiplyMatrix(Links)
        self.Tfinal = zeros((4,4))
        self.Tfinal[0,0:3] = (T[0:3,0:3]*manipdir).transpose()
        self.Tfinal[0:3,3] = T[0:3,0:3]*manippos+T[0:3,3]
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)

        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 5:
            raise self.CannotSolveError('need 5 joints')
        
        log.info('ikfast translation direction 5d: %r, direction=%r', solvejointvars, manipdir)
        
        # if last two axes are intersecting, can divide computing position and direction
        ilinks = [i for i,Tlink in enumerate(Links) if self.has(Tlink,*solvejointvars)]
        T = self.multiplyMatrix(Links[ilinks[-2]:])
        P = T[0:3,0:3]*manippos+T[0:3,3]
        D = T[0:3,0:3]*manipdir
        tree = None
        if not self.has(P,*solvejointvars):
            Tposinv = eye(4)
            Tposinv[0:3,3] = -P
            T0links=[Tposinv]+Links[:ilinks[-2]]
            try:
                log.info('last 2 axes are intersecting')
                tree = self.solve5DIntersectingAxes(T0links,manippos,D,solvejointvars,endbranchtree)
            except self.CannotSolveError, e:
                log.warn('%s', e)

        if tree is None:
            rawpolyeqs2 = [None]*len(solvejointvars)
            coupledsolutions = None
            endbranchtree2 = []
            for solvemethod in [self.solveLiWoernleHiller, self.solveKohliOsvatic]:#, self.solveManochaCanny]:
                if coupledsolutions is not None:
                    break
                for index in [2,3]:
                    T0links=LinksInv[:ilinks[index]][::-1]
                    T0 = self.multiplyMatrix(T0links)
                    T1links=Links[ilinks[index]:]
                    T1 = self.multiplyMatrix(T1links)
                    p0 = T0[0:3,0:3]*self.Tee[0:3,3]+T0[0:3,3]
                    p1 = T1[0:3,0:3]*manippos+T1[0:3,3]
                    l0 = T0[0:3,0:3]*self.Tee[0,0:3].transpose()
                    l1 = T1[0:3,0:3]*manipdir

                    AllEquations = []
                    for i in range(3):
                        AllEquations.append(self.SimplifyTransform(p0[i]-p1[i]).expand())
                        AllEquations.append(self.SimplifyTransform(l0[i]-l1[i]).expand())
                        
                    # check if all joints in solvejointvars[index:] are revolute and oriented in the same way
                    checkorientationjoints = None
                    leftside = None
                    if len(solvejointvars[:index]) == 3 and all([self.IsHinge(j.name) for j in solvejointvars[:index]]):
                        Taccums = None
                        for T in T0links:
                            if self.has(T, solvejointvars[0]):
                                Taccums = [T]
                            elif Taccums is not None:
                                Taccums.append(T)
                            if self.has(T, solvejointvars[index-1]):
                                break
                        if Taccums is not None:
                            Tcheckorientation = self.multiplyMatrix(Taccums)
                            checkorientationjoints = solvejointvars[:index]
                            leftside = True
                    if len(solvejointvars[index:]) == 3 and all([self.IsHinge(j.name) for j in solvejointvars[index:]]):
                        Taccums = None
                        for T in T1links:
                            if self.has(T, solvejointvars[index]):
                                Taccums = [T]
                            elif Taccums is not None:
                                Taccums.append(T)
                            if self.has(T, solvejointvars[-1]):
                                break
                        if Taccums is not None:
                            Tcheckorientation = self.multiplyMatrix(Taccums)
                            checkorientationjoints = solvejointvars[index:]
                            leftside = False
                    newsolvejointvars = solvejointvars
                    if checkorientationjoints is not None:
                        # TODO, have to consider different signs of the joints
                        cvar3 = cos(checkorientationjoints[0] + checkorientationjoints[1] + checkorientationjoints[2]).expand(trig=True)
                        svar3 = sin(checkorientationjoints[0] + checkorientationjoints[1] + checkorientationjoints[2]).expand(trig=True)
                        # to check for same orientation, see if T's rotation is composed of cvar3 and svar3
                        sameorientation = True
                        for i in range(3):
                            for j in range(3):
                                if Tcheckorientation[i,j] != S.Zero and not self.equal(Tcheckorientation[i,j], cvar3) and not self.equal(Tcheckorientation[i,j], -cvar3) and not self.equal(Tcheckorientation[i,j], svar3) and not self.equal(Tcheckorientation[i,j], -svar3) and Tcheckorientation[i,j] != S.One:
                                    sameorientation = False
                                    break
                        if sameorientation:
                            log.info('found joints %r to have same orientation, adding more equations', checkorientationjoints)
                            sumjoint = Symbol('j100')
                            for i in range(3):
                                for j in range(3):
                                    if self.equal(Tcheckorientation[i,j], cvar3):
                                        Tcheckorientation[i,j] = cos(sumjoint)
                                    elif self.equal(Tcheckorientation[i,j], -cvar3):
                                        Tcheckorientation[i,j] = -cos(sumjoint)
                                    elif self.equal(Tcheckorientation[i,j], svar3):
                                        Tcheckorientation[i,j] = sin(sumjoint)
                                    elif self.equal(Tcheckorientation[i,j], -svar3):
                                        Tcheckorientation[i,j] = -sin(sumjoint)
                            
                            if not leftside:
                                newT1links=[Tcheckorientation] + Links[ilinks[-1]+1:]
                                newT1 = self.multiplyMatrix(newT1links)
                                newp1 = newT1[0:3,0:3]*manippos+newT1[0:3,3]
                                newl1 = newT1[0:3,0:3]*manipdir
                                newp1 = newp1.subs(sin(checkorientationjoints[2]), sin(sumjoint - checkorientationjoints[0] - checkorientationjoints[1]).expand(trig=True)).expand()
                                newl1 = newl1.subs(sin(checkorientationjoints[2]), sin(sumjoint - checkorientationjoints[0] - checkorientationjoints[1]).expand(trig=True)).expand()
                                for i in range(3):
                                    newp1[i] = self.trigsimp(newp1[i], [sumjoint, checkorientationjoints[0], checkorientationjoints[1]])
                                    newl1[i] = self.trigsimp(newl1[i], [sumjoint, checkorientationjoints[0], checkorientationjoints[1]])

                                for i in range(3):
                                    AllEquations.append(self.SimplifyTransform(p0[i]-newp1[i]).expand())
                                    AllEquations.append(self.SimplifyTransform(l0[i]-newl1[i]).expand())
                                AllEquations.append(checkorientationjoints[0] + checkorientationjoints[1] + checkorientationjoints[2] - sumjoint)
                                AllEquations.append((sin(checkorientationjoints[0] + checkorientationjoints[1]) - sin(sumjoint-checkorientationjoints[2])).expand(trig=True))
                                AllEquations.append((cos(checkorientationjoints[0] + checkorientationjoints[1]) - cos(sumjoint-checkorientationjoints[2])).expand(trig=True))
                                AllEquations.append((sin(checkorientationjoints[1] + checkorientationjoints[2]) - sin(sumjoint-checkorientationjoints[0])).expand(trig=True))
                                AllEquations.append((cos(checkorientationjoints[1] + checkorientationjoints[2]) - cos(sumjoint-checkorientationjoints[0])).expand(trig=True))
                                AllEquations.append((sin(checkorientationjoints[2] + checkorientationjoints[0]) - sin(sumjoint-checkorientationjoints[1])).expand(trig=True))
                                AllEquations.append((cos(checkorientationjoints[2] + checkorientationjoints[0]) - cos(sumjoint-checkorientationjoints[1])).expand(trig=True))
                                for consistentvalues in self.testconsistentvalues:
                                    var = self.Variable(sumjoint)
                                    consistentvalues += var.getsubs((checkorientationjoints[0] + checkorientationjoints[1] + checkorientationjoints[2]).subs(consistentvalues))
                                newsolvejointvars = solvejointvars + [sumjoint]
                    self.sortComplexity(AllEquations)
                    
                    if rawpolyeqs2[index] is None:
                        rawpolyeqs2[index] = self.buildRaghavanRothEquations(p0,p1,l0,l1,solvejointvars)
                    try:
                        coupledsolutions,usedvars = solvemethod(rawpolyeqs2[index],newsolvejointvars,endbranchtree=[AST.SolverSequence([endbranchtree2])], AllEquationsExtra=AllEquations)
                        break
                    except self.CannotSolveError, e:
                        log.warn('%s', e)
                        continue
                    
            if coupledsolutions is None:
                raise self.CannotSolveError('raghavan roth equations too complex')
            
            log.info('solved coupled variables: %s',usedvars)
            if len(usedvars) < len(solvejointvars):
                curvars=solvejointvars[:]
                solsubs = self.freevarsubs[:]
                for var in usedvars:
                    curvars.remove(var)
                    solsubs += self.Variable(var).subs
                self.checkSolvability(AllEquations,curvars,self.freejointvars+usedvars)
                localtree = self.SolveAllEquations(AllEquations,curvars=curvars,othersolvedvars = self.freejointvars+usedvars,solsubs = solsubs,endbranchtree=endbranchtree)
                # make it a function so compiled code is smaller
                endbranchtree2.append(AST.SolverFunction('innerfn', self.verifyAllEquations(AllEquations,curvars,solsubs,localtree)))
                tree = coupledsolutions
            else:
                endbranchtree2 += endbranchtree
                tree = coupledsolutions
                
        chaintree = AST.SolverIKChainRay([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], Pee=(self.Tee[0:3,3]-self.Tee[0,0:3].transpose()*offsetdist).subs(self.freevarsubs), Dee=self.Tee[0,0:3].transpose().subs(self.freevarsubs),jointtree=tree,Dfk=self.Tfinal[0,0:3].transpose(),Pfk=self.Tfinal[0:3,3],is5dray=True)
        chaintree.dictequations += self.ppsubs
        return chaintree

    def solve5DIntersectingAxes(self, T0links, manippos, D, solvejointvars, endbranchtree):
        LinksInv = [self.affineInverse(T) for T in T0links]
        T0 = self.multiplyMatrix(T0links)
        Tmanipposinv = eye(4)
        Tmanipposinv[0:3,3] = -manippos
        T1links = [Tmanipposinv]+LinksInv[::-1]+[self.Tee]
        T1linksinv = [self.affineInverse(Tmanipposinv)]+T0links[::-1]+[self.Teeinv]
        AllEquations = self.buildEquationsFromPositions(T1links,T1linksinv,solvejointvars,self.freejointvars,uselength=True)
        transvars = [v for v in solvejointvars if self.has(T0,v)]
        self.checkSolvability(AllEquations,transvars,self.freejointvars)
        dirtree = []
        newendbranchtree = [AST.SolverSequence([dirtree])]
        transtree = self.SolveAllEquations(AllEquations,curvars=transvars[:],othersolvedvars=self.freejointvars,solsubs = self.freevarsubs[:],endbranchtree=newendbranchtree)
        transtree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,transtree)
        rotvars = [v for v in solvejointvars if self.has(D,v)]
        solsubs = self.freevarsubs[:]
        for v in transvars:
            solsubs += self.Variable(v).subs
        AllEquations = self.buildEquationsFromTwoSides([D],[T0[0:3,0:3].transpose()*self.Tee[0,0:3].transpose()],solvejointvars,uselength=False)        
        self.checkSolvability(AllEquations,rotvars,self.freejointvars+transvars)
        localdirtree = self.SolveAllEquations(AllEquations,curvars=rotvars[:],othersolvedvars = self.freejointvars+transvars,solsubs=solsubs,endbranchtree=endbranchtree)
        # make it a function so compiled code is smaller
        dirtree.append(AST.SolverFunction('innerfn', self.verifyAllEquations(AllEquations,rotvars,solsubs,localdirtree)))
        return transtree

    def solveFullIK_6D(self, LinksRaw, jointvars, isolvejointvars,Tmanipraw=eye(4)):
        """Solves the full 6D translation + rotation IK
        """
        from ikfast_AST import AST
        self._iktype = 'transform6d'
        Tgripper = eye(4)
        for i in range(4):
            for j in range(4):
                Tgripper[i,j] = self.convertRealToRational(Tmanipraw[i,j])
        Tfirstright = LinksRaw[-1]*Tgripper
        Links = LinksRaw[:-1]
        #         if Links[0][0:3,0:3] == eye(3):
        #             # first axis is prismatic, so zero out self.Tee
        #             for i in range(3):
        #                 if Links[0][i,3] != S.Zero:
        #                     self.Tee[i,3] = S.Zero
        #             self.Teeinv = self.affineInverse(self.Tee)
    
        # take inverse for each link matrix
        LinksInv = [self.affineInverse(link) for link in Links]
        # take product of all link matrices
        self.Tfinal = self.multiplyMatrix(Links)

        # plug simple pre-set values into forward kinematics formulas
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)

        # construct a SolverStoreSolution object
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        if len(solvejointvars) != 6:
            raise self.CannotSolveError('need 6 joints')
        log.info('ikfast 6d: %s',solvejointvars)

        # check if some set of three consecutive axes intersect at one point
        # if so, the IK solution will be easier to derive
        tree = self.TestIntersectingAxes(solvejointvars, Links, LinksInv, endbranchtree)
        if tree is None:
            sliderjointvars = [var for var in solvejointvars if not self.IsHinge(var.name)]
            if len(sliderjointvars) > 0:
                ZeroMatrix = zeros(4)
                for i,Tlink in enumerate(Links):
                    if self.has(Tlink,*sliderjointvars):
                        # try sliding left
                        if i > 0:
                            ileftsplit = None
                            for isplit in range(i-1,-1,-1):
                                M = self.multiplyMatrix(Links[isplit:i])
                                if M*Tlink-Tlink*M != ZeroMatrix:
                                    break
                                if self.has(M,*solvejointvars):
                                    # surpassed a variable!
                                    ileftsplit = isplit
                            if ileftsplit is not None:
                                # try with the new order
                                log.info('rearranging Links[%d] to Links[%d]',i,ileftsplit)
                                NewLinks = list(Links)
                                NewLinks[(ileftsplit+1):(i+1)] = Links[ileftsplit:i]
                                NewLinks[ileftsplit] = Links[i]
                                NewLinksInv = list(LinksInv)
                                NewLinksInv[(ileftsplit+1):(i+1)] = Links[ileftsplit:i]
                                NewLinksInv[ileftsplit] = LinksInv[i]
                                tree = self.TestIntersectingAxes(solvejointvars,NewLinks, NewLinksInv,endbranchtree)
                                if tree is not None:
                                    break
                        # try sliding right                            
                        if i+1 < len(Links):
                            irightsplit = None
                            for isplit in range(i+1,len(Links)):
                                M = self.multiplyMatrix(Links[i+1:(isplit+1)])
                                if M*Tlink-Tlink*M != ZeroMatrix:
                                    break
                                if self.has(M,*solvejointvars):
                                    # surpassed a variable!
                                    irightsplit = isplit
                            if irightsplit is not None:
                                log.info('rearranging Links[%d] to Links[%d]',i,irightsplit)
                                # try with the new order
                                NewLinks = list(Links)
                                NewLinks[i:irightsplit] = Links[(i+1):(irightsplit+1)]
                                NewLinks[irightsplit] = Links[i]
                                NewLinksInv = list(LinksInv)
                                NewLinksInv[i:irightsplit] = LinksInv[(i+1):(irightsplit+1)]
                                NewLinksInv[irightsplit] = LinksInv[i]
                                tree = self.TestIntersectingAxes(solvejointvars,NewLinks, NewLinksInv,endbranchtree)
                                if tree is not None:
                                    break
        if tree is None:
            linklist = list(self.iterateThreeNonIntersectingAxes(solvejointvars,Links, LinksInv))
            # first try LiWoernleHiller since it is most robust
            for ilinklist, (T0links, T1links) in enumerate(linklist):
                log.info('try first group %d/%d', ilinklist, len(linklist))
                try:
                    # if T1links[-1] doesn't have any symbols, put it over to T0links. Since T1links has the position unknowns, putting over the coefficients to T0links makes things simpler
                    if not self.has(T1links[-1], *solvejointvars):
                        T0links.append(self.affineInverse(T1links.pop(-1)))
                    tree = self.solveFullIK_6DGeneral(T0links, T1links, solvejointvars, endbranchtree, usesolvers=1)
                    break
                except (self.CannotSolveError,self.IKFeasibilityError), e:
                    log.warn('%s',e)
            
            if tree is None:
                log.info('trying the rest of the general ik solvers')
                for ilinklist, (T0links, T1links) in enumerate(linklist):
                    log.info('try second group %d/%d', ilinklist, len(linklist))
                    try:
                        # If T1links[-1] has no symbols, then we put it over to T0links.
                        # Since T1links has the position unknowns, doing so simplifies computations.
                        if not self.has(T1links[-1], *solvejointvars):
                            T0links.append(self.affineInverse(T1links.pop(-1)))
                        tree = self.solveFullIK_6DGeneral(T0links, T1links, solvejointvars, endbranchtree, usesolvers=6)
                        break
                    except (self.CannotSolveError,self.IKFeasibilityError), e:
                        log.warn('%s',e)
                
        if tree is None:
            raise self.CannotSolveError('cannot solve 6D mechanism!')
        
        chaintree = AST.SolverIKChainTransform6D([(jointvars[ijoint],ijoint) for ijoint in isolvejointvars], \
                                                 [(v,i) for v,i in izip(self.freejointvars, self.ifreejointvars)], \
                                                 (self.Tee*self.affineInverse(Tfirstright)).subs(self.freevarsubs), \
                                                 tree, \
                                                 Tfk = self.Tfinal*Tfirstright)
        chaintree.dictequations += self.ppsubs+self.npxyzsubs+self.rxpsubs
        return chaintree
    
    def TestIntersectingAxes(self,solvejointvars,Links,LinksInv,endbranchtree):
        for T0links, T1links, transvars, rotvars, solveRotationFirst in \
            self.iterateThreeIntersectingAxes(solvejointvars, Links, LinksInv): # generator
            try:
                return self.solve6DIntersectingAxes(T0links, T1links, transvars, rotvars, \
                                                    solveRotationFirst=solveRotationFirst, \
                                                    endbranchtree=endbranchtree)
            except (self.CannotSolveError,self.IKFeasibilityError), e:
                log.warn('%s',e)
        return None

    def _ExtractTranslationsOutsideOfMatrixMultiplication(self, Links, LinksInv, solvejointvars):
        """
        Try to extract translations outside of the multiplication, from both left and right,
        i.e., find and return 

        Tlefttrans, NewLinks, Trighttrans

        such that

        Tlefttrans * MultiplyMatrix(Links_OUT) * Trighttrans = MultiplyMatrix(Links_IN).

        Tleftrans and Trighttrans are purely translation matrices in form [resp.]

        [ I_3 | p_1 ]       [ I_3 | p_2 ]
        -------------  and  -------------
        [  0  |  1  ]       [  0  |  1  ]

        where p_1, p_2 do not depend on solvejointvars.

        Since the first and last matrices MUST contain solvejointvars, we only
        modify the 2nd and penultimate matrices of Links.

        This is equivalent to finding 

        p = p_1 + R*p_2 + p'

        where MultiplyMatrix(Links_IN) and MultiplyMatrix(Links_OUT) are

        [ R | p ]       [ R | p']
        ---------  and  ---------  respectively.
        [ 0 | 1 ]       [ 0 | 1 ]

        """

        # this is doing shallow copy, so redundant???
        NewLinks = list(Links)
        assert(id(NewLinks)!=id(Links))
        assert(id(NewLinks[0])!=id(Links[1]))

        # deep copy their values before they get modified
        a = Links[1][:,:]
        b = Links[-2][:,:]        
        
        # initialize T_left_trans, T_right_trans, and Temp
        Tlefttrans  = eye(4)
        Trighttrans = eye(4)
        Temp        = zeros(4)
        
        # work on the product of the first two matrices to find T_left_trans
        """
        separated_trans = Links[0][0:3,0:3] * Links[1][0:3,3]
        for j in range(0,3):
            if not separated_trans[j].has(*solvejointvars):
                Tlefttrans[j,3] = separated_trans[j]
        """

        Tlefttrans[0:3,3] = Links[0][0:3,0:3] * Links[1][0:3,3]
        for j in range(0,3):
            if Tlefttrans[j].has(*solvejointvars):
                Tlefttrans[j,3] = S.Zero

        # work on the product of the last two matrices to find T_right_trans
        #
        # original:
        #
        # Trighttrans[0:3,3] = Links[-2][0:3,0:3].transpose() * Links[-2][0:3,3]
        # Trot_with_trans = Trighttrans * Links[-1]
        # separated_trans = Trot_with_trans[0:3,0:3].transpose() * Trot_with_trans[0:3,3]
        #
        # first iteration:
        # separated_trans = Links[-1][0:3,0:3].transpose() * \
        #                  ( Links[-2][0:3,0:3].transpose()*Links[-2][0:3,3]+Links[-1][0:3,3])
        # second iteration:
        separated_trans = -LinksInv[-2][0:3,0:3]*LinksInv[-1][0:3,3]-LinksInv[-2][0:3,3]
        
        for j in range(0,3):
            if separated_trans[j].has(*solvejointvars):
                Trighttrans[j,3] = S.Zero
            else:
                Trighttrans[j,3] = separated_trans[j]
                
        """
        if any(Tlefttrans-eye(4)):
               print 'T_left_trans', Tlefttrans
                       
        if any(Trighttrans-eye(4)):
               print 'T_right_trans', Trighttrans
        """

        # update the second matrix
        Temp[0:3,3] = Tlefttrans[0:3,3];        
        Links[1] -= Temp

        # update the penultimate (second last) matrix
        Temp[0:3,3] = Links[-2][0:3,0:3]*Trighttrans[0:3,3];
        Links[-2] -= Temp

        # TGN adds mathematically equivalent formulas for checking
        # print 'old left: ', self.affineInverse(Tlefttrans)*a
        a[0:3,3] -= Tlefttrans[0:3,3]
        # print 'new left: ', a
        # print 'old right: ', b*self.affineInverse(Trighttrans)
        b[0:3,3] -= b[0:3,0:3]*Trighttrans[0:3,3]
        # print 'new right: ', b
        assert(not any(a-Links[1] ))
        # print "b = ", b
        # print "NewLinks[-2] = ", NewLinks[-2]
        assert(not any(b-Links[-2]))
        
        return Tlefttrans, Trighttrans

    def iterateThreeIntersectingAxes(self, solvejointvars, Links, LinksInv):
        """
        This generator searches for 3 consecutive intersecting axes. 
        If a robot has this condition, it makes IK computations much simpler.

        Called by TestIntersectingAxes only.
        """
        TestLinks=Links
        TestLinksInv=LinksInv
        # extract indices for matrices that contain joint variables
        ilinks = [i for i,Tlink in enumerate(TestLinks) if self.has(Tlink,*solvejointvars)]
        hingejointvars = [var for var in solvejointvars if self.IsHinge(var.name)]
        polysymbols = []
        for solvejointvar in solvejointvars:
            polysymbols += [s[0] for s in self.Variable(solvejointvar).subs]

        num_of_combination = len(ilinks)-2
        for i in range(num_of_combination):
            startindex = ilinks[i]
            endindex   = ilinks[i+2]+1

            T0links    = TestLinks[startindex:endindex]
            # There are exactly three joint variables in T0links, one in the first matrix, on in the last.
            # To isolate the left and right translation parts that are independent of solvejointvars,
            # we examine the 2nd and 2nd last matrices in T0links, respectively.

            #if startindex is 0:
            #    T0linksInv = TestLinksInv[endindex-1::-1]
            #else:
            #    T0linksInv = TestLinksInv[endindex-1:startindex-1:-1]
            T0linksInv = TestLinksInv[startindex:endindex][::-1]

            Tlefttrans, Trighttrans = self._ExtractTranslationsOutsideOfMatrixMultiplication \
                                      ( T0links, T0linksInv, solvejointvars )
            # T0links can be modified by the above call; T0linksInv does not change
            T0 = self.multiplyMatrix(T0links)

            # count number of variables in T0[0:3,0:3]
            numVariablesInRotation = sum([self.has(T0[0:3,0:3],solvejointvar) \
                                          for solvejointvar in solvejointvars])
            if numVariablesInRotation < 3:
                assert(numVariableInRotation is 3)
                continue
            solveRotationFirst = False

            # (was RD's comments; TGN changed the wording)
            # Sometimes three axes intersect but intersecting condition isn't satisfied ONLY due to machine epsilon,
            # so we use RoundEquationsTerms to set S.Zero to any coefficients in T0[:3,3] below some threshold
            translationeqs = [self.RoundEquationTerms(eq.expand()) for eq in T0[0:3,3]]
            # TGN: inconsistency in code, should decide whether to write 0:3 or merely :3

            if self.has(translationeqs, *hingejointvars):
                # first attempt does not succeed, so we try working on T0linksInv
                Tlefttrans, Trighttrans = self._ExtractTranslationsOutsideOfMatrixMultiplication \
                                          ( T0linksInv, T0links, solvejointvars )
                # T0linksInv can be modified by the above call; T0links does not change
                T0 = self.multiplyMatrix(T0linksInv)
                            
                translationeqs = [self.RoundEquationTerms(eq.expand()) for eq in T0[:3,3]]
                if not self.has(translationeqs,*hingejointvars):
                    T1links = TestLinks[endindex:]
                    # A_e, A_{e+1}, ..., A_{n-1}
                    if len(T1links) > 0:
                        # left multiply Trighttrans onto A_e
                        # T1links[0] = Trighttrans * T1links[0] 
                        T1links[0][0:3,3] += Trighttrans[0:3,3]
                    else:
                        assert(endindex is len(TestLinks))
                        T1links = [Trighttrans]

                    # append inv(Tee), A_0, A_1, ..., A_{s-1}
                    T1links.append(self.Teeinv)
                    T1links += TestLinks[:startindex]
                    # So T1links reads
                    #
                    # A_e, A_{e+1}, ..., A_{n-1}, inv(Tee), A_0, A_1, ..., A_{s-1}
                    #
                    # while T0linksInv contains inv(A_{e-1}), ..., inv(A_{s+1}), inv(A_s)
                    #
                    # where inv(A_{e-2}) and inv(A_{s+1}) are updated by _ExtractTranslationsOutsideOfMatrixMultiplication
                    
                    # right multiply Tlefttrans onto A_{s-1}
                    # T1links[-1] = T1links[-1] * Tlefttrans
                    Tlefttrans[0:3,3] = T1links[-1][0:3,0:3]*Tlefttrans[0:3,3]
                    T1links[-1][0:3,3] += Tlefttrans[0:3,3]
                    solveRotationFirst = True
            else:
                # first attempt succeeds as translation eqns don't depend on hingejointvars
                #
                #  A_s * A_{s+1} * ... A_{e-1} = Tlefttrans * prod(T0links_NEW) * Trighttrans

                T1links = TestLinksInv[:startindex][::-1]
                # inv(A_{s-1}), ..., inv(A_1), inv(A_0)
                if len(T1links) > 0:
                    # left multiply inv(Tlefttrans) onto inv(A_{s-1})
                    # T1links[0] = self.affineInverse(Tlefttrans) * T1links[0]
                    T1links[0][0:3,3] -= Tlefttrans[0:3,3]
                else:
                    assert(startindex is 0)
                    # T1links = [self.affineInverse(Tlefttrans)] 
                    Tlefttrans[0:3,3] = -Tlefttrans[0:3,3]
                    T1links = [Tlefttrans]

                # append Tee, inv(A_{n-1}), ..., inv(A_{e+1}), inv(A_e)
                T1links.append(self.Tee)
                T1links += TestLinksInv[endindex:][::-1]
                # So T1links reads
                #
                # inv(A_{s-1}), ..., inv(A_1), inv(A_0), Tee, inv(A_{n-1}), ..., inv(A_{e+1}), inv(A_e)
                #
                # while T0links contains A_s, A_{s+1}, ..., A_{e-2}, A_{e-1}
                #
                # where A_{s+1} and A_{e-2} are updated by _ExtractTranslationsOutsideOfMatrixMultiplication
                
                # right multiply inv(Trighttrans) onto inv(A_e)
                # T1links[-1] = T1links[-1] * self.affineInverse(Trighttrans)
                Trighttrans[0:3,3] = T1links[-1][0:3,0:3]*Trighttrans[0:3,3]
                T1links[-1][0:3,3] -= Trighttrans[0:3,3]
                
                solveRotationFirst = True

            if solveRotationFirst:
                # collect rotation and translation variables
                rotvars   = []
                transvars = []
                for solvejointvar in solvejointvars:
                    if self.has(T0[0:3,0:3],solvejointvar):
                        rotvars.append(solvejointvar)
                    else:
                        transvars.append(solvejointvar)
                        
                if len(rotvars) == 3 and len(transvars) == 3:
                    log.info('found 3 consecutive intersecting axes links[%d:%d]\n' + \
                             '        rotn_vars = %s\n' + \
                             '        trns_vars = %s', \
                             startindex, endindex, rotvars,transvars)
                    # generator reports only one set of 3 axes at a time
                    yield T0links, T1links, transvars, rotvars, solveRotationFirst

    def RoundEquationTerms(self, eq, epsilon=None):
        """
        Recursively go down the computational graph, and round constants below epsilon as S.Zero
        """
        
        # TGN moved it here
        if epsilon is None:
            epsilon = 5*(10**-self.precision)

        if eq.is_Add: # ..+..-..+..
            neweq = S.Zero
            for subeq in eq.args:
                neweq += self.RoundEquationTerms(subeq,epsilon)
                
        elif eq.is_Mul: # ..*../..*..
            neweq = self.RoundEquationTerms(eq.args[0],epsilon)
            for subeq in eq.args[1:]:
                neweq *= self.RoundEquationTerms(subeq,epsilon)
                
        elif eq.is_Function: # for sin, cos, etc.
            newargs = [self.RoundEquationTerms(subeq,epsilon) for subeq in eq.args]
            neweq = eq.func(*newargs)
            
        elif eq.is_number:
            # TGN: the rounding happens here. Since epsilon is only checked and modified here
            #      with the same value each time, we can move its assignment to top
            #if epsilon is None:
            #    epsilon = 5*(10**-self.precision)
            if abs(eq.evalf()) <= epsilon: # <= or < ?
                # print "rounding ", eq.evalf(), " to S.Zero"
                neweq = S.Zero
            else:
                neweq = eq
        else:
            neweq=eq
        return neweq

    def RoundPolynomialTerms(self,peq,epsilon):
        terms = {}
        for monom, coeff in peq.terms():
            if not coeff.is_number or abs(coeff) > epsilon:
                terms[monom] = coeff
        if len(terms) == 0:
            return Poly(S.Zero,peq.gens)

        return peq.from_dict(terms, *peq.gens)

    def iterateThreeNonIntersectingAxes(self, solvejointvars, Links, LinksInv):
        """
        check for three consecutive non-intersecting axes.
        if several points exist, so have to choose one that is least complex?
        """
        ilinks = [i for i,Tlink in enumerate(Links) if self.has(Tlink,*solvejointvars)]
        usedindices = []
        for imode in range(2):
            for i in range(len(ilinks)-2):
                if i in usedindices:
                    continue
                startindex = ilinks[i]
                endindex = ilinks[i+2]+1
                p0 = self.multiplyMatrix(Links[ilinks[i]:ilinks[i+1]])[0:3,3]
                p1 = self.multiplyMatrix(Links[ilinks[i+1]:ilinks[i+2]])[0:3,3]
                has0 = self.has(p0,*solvejointvars)
                has1 = self.has(p1,*solvejointvars)
                if (imode == 0 and has0 and has1) or (imode == 1 and (has0 or has1)):
                    T0links = Links[startindex:endindex]
                    T1links = LinksInv[:startindex][::-1]
                    T1links.append(self.Tee)
                    T1links += LinksInv[endindex:][::-1]
                    usedindices.append(i)
                    usedvars = [var for var in solvejointvars if any([self.has(T0,var) for T0 in T0links])]
                    log.info('found 3 consecutive non-intersecting axes links[%d:%d], vars=%s',startindex,endindex,str(usedvars))
                    yield T0links, T1links

    def solve6DIntersectingAxes(self, T0links, T1links, transvars, rotvars, solveRotationFirst, endbranchtree):
        """
        Solve 6D equations where 3 axes intersect at a point.
        These axes correspond to T0links; we use them to compute the orientation.
        The remaining 3 axes correspond to T1links; we use them to compute the position first.

        Called by TestIntersectingAxes only.
        """
        from ikfast_AST import AST
        
        self._iktype = 'transform6d'
        assert(len(transvars)==3 and len(rotvars) == 3)
        T0 = self.multiplyMatrix(T0links)
        T0posoffset = eye(4)
        T0posoffset[0:3,3] = -T0[0:3,3]
        T0links = [T0posoffset] + T0links
        T1links = [T0posoffset] + T1links
        T1 = self.multiplyMatrix(T1links)

        # TGN: getting into this function means solveRotationFirst is True?
        assert(solveRotationFirst)
        
        # othersolvedvars = rotvars + self.freejointvars if solveRotationFirst else self.freejointvars[:]
        # in original code, solveRotationFirst is either None or False
        othersolvedvars = self.freejointvars[:]
        T1linksinv = [self.affineInverse(T) for T in T1links]
        AllEquations = self.buildEquationsFromPositions(T1links, T1linksinv, \
                                                        transvars, othersolvedvars, \
                                                        uselength = True)

        # TGN: This function simply passes
        self.checkSolvability(AllEquations, transvars, self.freejointvars)
        
        rottree = []
        #if solveRotationFirst:
        #    # can even get here?? it is either None or False
        #    assert(0)
        #    newendbranchtree = endbranchtree
        #else:

        # call IKFastSolver.SolverSequence constructor        
        newendbranchtree = [AST.SolverSequence([rottree])]

        # current variables (translation)
        curvars = transvars[:]
        # known values we can plug in
        solsubs = self.freevarsubs[:]

        transtree = self.SolveAllEquations(AllEquations, \
                                           curvars = curvars, \
                                           othersolvedvars = othersolvedvars[:], \
                                           solsubs = solsubs, \
                                           endbranchtree = newendbranchtree)

        transtree = self.verifyAllEquations(AllEquations, \
                                            transvars+rotvars, \
                                            # rotvars if solveRotationFirst \
                                            # else transvars+rotvars, \
                                            self.freevarsubs[:], transtree)

        solvertree = []
        solvedvarsubs = self.freevarsubs[:]
        #if solveRotationFirst:
        #    # can even get here?? it is either None or False
        #    assert(0)
        #    storesolutiontree = transtree
        #else:
        solvertree += transtree
        storesolutiontree = endbranchtree
        for tvar in transvars:
            solvedvarsubs += self.Variable(tvar).subs
                
        Ree = zeros((3,3))
        for i in range(3):
            for j in range(3):
                Ree[i,j] = Symbol('new_r%d%d'%(i,j))
        try:
            T1sub = T1.subs(solvedvarsubs)
            othersolvedvars = self.freejointvars if solveRotationFirst else transvars+self.freejointvars
            AllEquations = self.buildEquationsFromRotation(T0links, Ree, rotvars, othersolvedvars)
            self.checkSolvability(AllEquations, rotvars, othersolvedvars)
            currotvars = rotvars[:]
            rottree += self.SolveAllEquations(AllEquations, \
                                              curvars = currotvars, \
                                              othersolvedvars = othersolvedvars, \
                                              solsubs = self.freevarsubs[:], \
                                              endbranchtree = storesolutiontree)
            # has to be after SolveAllEquations...?
            for i in range(3):
                for j in range(3):
                    self.globalsymbols.append((Ree[i,j],T1sub[i,j]))

            if len(rottree) == 0:
                raise self.CannotSolveError('could not solve for all rotation variables: %s:%s' % \
                                            (str(freevar), str(freevalue)))

            #if solveRotationFirst:
            #    solvertree.append(AST.SolverRotation(T1sub, rottree))
            #else:
            rottree[:] = [AST.SolverRotation(T1sub, rottree[:])]
            return solvertree
        finally:
            # remove the Ree global symbols
            removesymbols = set()
            for i in range(3):
                for j in range(3):
                    removesymbols.add(Ree[i,j])
            self.globalsymbols = [g for g in self.globalsymbols if not g[0] in removesymbols]
            
    def solveFullIK_6DGeneral(self, T0links, T1links, solvejointvars, endbranchtree, usesolvers=7):
        """Solve 6D equations of a general kinematics structure.
        This method only works if there exists 3 consecutive joints that do not always intersect!
        """
        self._iktype = 'transform6d'
        rawpolyeqs2 = [None,None]
        coupledsolutions = None
        leftovervarstree = []
        origendbranchtree = endbranchtree
        solvemethods = []
        if usesolvers & 1:
            solvemethods.append(self.solveLiWoernleHiller)
        if usesolvers & 2:
            solvemethods.append(self.solveKohliOsvatic)
        if usesolvers & 4:
            solvemethods.append(self.solveManochaCanny)
        for solvemethod in solvemethods:
            if coupledsolutions is not None:
                break
            complexities = [0,0]
            for splitindex in [0, 1]:
                if rawpolyeqs2[splitindex] is None:
                    if splitindex == 0:
                        # invert, this seems to always give simpler solutions, so prioritize it
                        T0 = self.affineSimplify(self.multiplyMatrix([self.affineInverse(T) for T in T0links][::-1]))
                        T1 = self.affineSimplify(self.multiplyMatrix([self.affineInverse(T) for T in T1links][::-1]))
                    else:
                        T0 = self.affineSimplify(self.multiplyMatrix(T0links))
                        T1 = self.affineSimplify(self.multiplyMatrix(T1links))
                    rawpolyeqs,numminvars = self.buildRaghavanRothEquationsFromMatrix(T0,T1,solvejointvars,simplify=False)
                    if numminvars <= 5 or len(rawpolyeqs[0][1].gens) <= 6:
                        rawpolyeqs2[splitindex] = rawpolyeqs
                complexities[splitindex] = sum([self.ComputePolyComplexity(peq0)+self.ComputePolyComplexity(peq1) for peq0, peq1 in rawpolyeqs2[splitindex]])
            # try the lowest complexity first and then simplify!
            sortedindices = sorted(zip(complexities,[0,1]))
            
            for complexity, splitindex in sortedindices:
                for peqs in rawpolyeqs2[splitindex]:
                    c = sum([self.codeComplexity(eq) for eq in peqs[0].coeffs()])
                    if c < 5000:
                        peqs[0] = self.SimplifyTransformPoly (peqs[0])
                    else:
                        log.info('skip simplification since complexity = %d...', c)
                    #self.codeComplexity(poly0.as_expr()) < 2000:
                    c = sum([self.codeComplexity(eq) for eq in peqs[1].coeffs()])
                    if c < 5000:
                        peqs[1] = self.SimplifyTransformPoly (peqs[1])
                    else:
                        log.info('skip simplification since complexity = %d...', c)
                try:
                    if rawpolyeqs2[splitindex] is not None:
                        rawpolyeqs=rawpolyeqs2[splitindex]
                        endbranchtree=[AST.SolverSequence([leftovervarstree])]
                        unusedsymbols = []
                        for solvejointvar in solvejointvars:
                            usedinequs = any([var in rawpolyeqs[0][0].gens or var in rawpolyeqs[0][1].gens for var in self.Variable(solvejointvar).vars])
                            if not usedinequs:
                                unusedsymbols += self.Variable(solvejointvar).vars
                        AllEquationsExtra = []
                        AllEquationsExtraPruned = [] # prune equations for variables that are not used in rawpolyeqs
                        for i in range(3):
                            for j in range(4):
                                # have to make sure that any variable not in rawpolyeqs[0][0].gens and rawpolyeqs[0][1].gens is not used
                                eq = self.SimplifyTransform(T0[i,j]-T1[i,j])
                                if not eq.has(*unusedsymbols):
                                    AllEquationsExtraPruned.append(eq)
                                AllEquationsExtra.append(eq)
                        self.sortComplexity(AllEquationsExtraPruned)
                        self.sortComplexity(AllEquationsExtra)
                        coupledsolutions,usedvars = solvemethod(rawpolyeqs,solvejointvars,endbranchtree=endbranchtree,AllEquationsExtra=AllEquationsExtraPruned)
                        break
                except self.CannotSolveError, e:
                    if rawpolyeqs2[splitindex] is not None and len(rawpolyeqs2[splitindex]) > 0:
                        log.warn(u'solving %s: %s', rawpolyeqs2[splitindex][0][0].gens, e)
                    else:
                        log.warn(e)
                    continue
                
        if coupledsolutions is None:
            raise self.CannotSolveError('6D general method failed, raghavan roth equations might be too complex')
        
        log.info('solved coupled variables: %s',usedvars)
        curvars=solvejointvars[:]
        solsubs = self.freevarsubs[:]
        for var in usedvars:
            curvars.remove(var)
            solsubs += self.Variable(var).subs
        if len(curvars) > 0:
            self.sortComplexity(AllEquationsExtra)
            self.checkSolvability(AllEquationsExtra,curvars,self.freejointvars+usedvars)
            leftovertree = self.SolveAllEquations(AllEquationsExtra, \
                                                  curvars = curvars, \
                                                  othersolvedvars = self.freejointvars+usedvars, \
                                                  solsubs = solsubs, \
                                                  endbranchtree = origendbranchtree)
            leftovervarstree.append(AST.SolverFunction('innerfn',leftovertree))
        else:
            leftovervarstree += origendbranchtree
        return coupledsolutions
    
    def solveFullIK_TranslationAxisAngle4D(self, LinksRaw, jointvars, isolvejointvars, \
                                           rawmanipdir  = Matrix(3,1,[S.One,S.Zero,S.Zero]),  \
                                           rawmanippos  = Matrix(3,1,[S.Zero,S.Zero,S.Zero]), \
                                           rawglobaldir = Matrix(3,1,[S.Zero,S.Zero,S.One]),  \
                                           rawglobalnormaldir = None,
                                           ignoreaxis = None, rawmanipnormaldir = None, \
                                           Tmanipraw = None):
        """Solves 3D translation + Angle with respect to an axis
        :param rawglobalnormaldir: the axis in the base coordinate system that will be computing a rotation about
        :param rawglobaldir: the axis normal to rawglobalnormaldir that represents the 0 angle.
        :param rawmanipnormaldir: the normal dir in the manip coordinate system for emasuring the 0 angle offset. complements rawglobalnormaldir, which shoudl be in the base coordinate system.
        :param rawmanipdir: the axis in the manip coordinate system measuring the in-plane angle with.
        :param rawmanippos: the position in manip effector coordinate system for measuring position
        :param Tmanipraw: extra transform of the manip coordinate system with respect to the end effector
        """
        self._iktype = 'translationaxisangle4d'
        globaldir = Matrix(3,1,[Float(x,30) for x in rawglobaldir])
        globaldir /= sqrt(globaldir[0]*globaldir[0]+globaldir[1]*globaldir[1]+globaldir[2]*globaldir[2])
        for i in range(3):
            globaldir[i] = self.convertRealToRational(globaldir[i],5)
        iktype = None
        if rawglobalnormaldir is not None:
            globalnormaldir = Matrix(3,1,[Float(x,30) for x in rawglobalnormaldir])
            binormaldir = globalnormaldir.cross(globaldir).transpose()
            if globaldir[0] == S.One and globalnormaldir[2] == S.One:
                if ignoreaxis == 2:
                    iktype = IkType.TranslationXYOrientation3D
                else:
                    iktype = IkType.TranslationXAxisAngleZNorm4D
            elif globaldir[1] == S.One and globalnormaldir[0] == S.One:
                iktype = IkType.TranslationYAxisAngleXNorm4D
            elif globaldir[2] == S.One and globalnormaldir[1] == S.One:
                iktype = IkType.TranslationZAxisAngleYNorm4D
        else:
            globalnormaldir = None
            if globaldir[0] == S.One:
                iktype = IkType.TranslationXAxisAngle4D
            elif globaldir[1] == S.One:
                iktype = IkType.TranslationYAxisAngle4D
            elif globaldir[2] == S.One:
                iktype = IkType.TranslationZAxisAngle4D

        if rawmanipnormaldir is None:
            manipnormaldir = globalnormaldir
        else:
            manipnormaldir = Matrix(3,1,[self.convertRealToRational(x) for x in rawmanipnormaldir])
        
        if iktype is None:
            raise ValueError('currently globaldir can only by one of x,y,z axes')
        
        manippos = Matrix(3,1,[self.convertRealToRational(x) for x in rawmanippos])
        manipdir = Matrix(3,1,[Float(x,30) for x in rawmanipdir])
        L = sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2])
        manipdir /= L
        for i in range(3):
            manipdir[i] = self.convertRealToRational(manipdir[i],5)
        manipdir /= sqrt(manipdir[0]*manipdir[0]+manipdir[1]*manipdir[1]+manipdir[2]*manipdir[2]) # unfortunately have to do it again...
        Links = LinksRaw[:]
        if Tmanipraw is not None:
            Links.append(self.RoundMatrix(self.GetMatrixFromNumpy(Tmanipraw)))
        
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        
        LinksInv = [self.affineInverse(link) for link in Links]
        Tallmult = self.multiplyMatrix(Links)
        self.Tfinal = zeros((4,4))
        if globalnormaldir is None:
            self.Tfinal[0,0] = acos(globaldir.dot(Tallmult[0:3,0:3]*manipdir))
        else:
            self.Tfinal[0,0] = atan2(binormaldir.dot(Tallmult[0:3,0:3]*manipdir), globaldir.dot(Tallmult[0:3,0:3]*manipdir))
        if self.Tfinal[0,0] == nan:
            raise self.CannotSolveError('cannot solve 4D axis angle IK. ' + \
                                        'Most likely manipulator direction is aligned with the rotation axis')
        
        self.Tfinal[0:3,3] = Tallmult[0:3,0:3]*manippos+Tallmult[0:3,3]
        self.testconsistentvalues = self.ComputeConsistentValues(jointvars,self.Tfinal,numsolutions=4)
        
        solvejointvars = [jointvars[i] for i in isolvejointvars]
        expecteddof = 4
        if ignoreaxis is not None:
            expecteddof -= 1
        if len(solvejointvars) != expecteddof:
            raise self.CannotSolveError('need %d joints'%expecteddof)
        
        log.info('ikfast translation axis %dd, globaldir=%s, manipdir=%s: %s', expecteddof, globaldir, manipdir, solvejointvars)
        
        # if last two axes are intersecting, can divide computing position and direction
        ilinks = [i for i,Tlink in enumerate(Links) if self.has(Tlink,*solvejointvars)]
        
        Tmanipposinv = eye(4)
        Tmanipposinv[0:3,3] = -manippos
        T1links = [Tmanipposinv]+LinksInv[::-1]+[self.Tee]
        T1linksinv = [self.affineInverse(Tmanipposinv)]+Links[::-1]+[self.Teeinv]
        AllEquations = self.buildEquationsFromPositions(T1links, \
                                                        T1linksinv,\
                                                        solvejointvars, \
                                                        self.freejointvars, \
                                                        uselength = True, \
                                                        ignoreaxis = ignoreaxis)
        
        if not all([abs(eq.subs(self.testconsistentvalues[0]).evalf())<=1e-10 for eq in AllEquations]):
            raise self.CannotSolveError('some equations are not consistent with the IK, double check if using correct IK type')
        
        for index in range(len(ilinks)):
            # inv(T0) * T1 * manipdir = globaldir
            # => T1 * manipdir = T0 * globaldir
            T0links=LinksInv[:ilinks[index]][::-1]
            T0 = self.multiplyMatrix(T0links)
            T1links=Links[ilinks[index]:]
            T1 = self.multiplyMatrix(T1links)
            globaldir2 = T0[0:3,0:3]*globaldir
            manipdir2 = T1[0:3,0:3]*manipdir
            for i in range(3):
                if globaldir2[i].is_number:
                    globaldir2[i] = self.convertRealToRational(globaldir2[i])
                if manipdir2[i].is_number:
                    manipdir2[i] = self.convertRealToRational(manipdir2[i])
            eq = self.SimplifyTransform(self.trigsimp(globaldir2.dot(manipdir2),solvejointvars))-cos(self.Tee[0])
            if self.CheckExpressionUnique(AllEquations,eq):
                AllEquations.append(eq)
            if globalnormaldir is not None:
                binormaldir2 = T0[0:3,0:3]*binormaldir
                for i in range(3):
                    if binormaldir2[i].is_number:
                        binormaldir2[i] = self.convertRealToRational(binormaldir2[i])
                eq = self.SimplifyTransform(self.trigsimp(binormaldir2.dot(manipdir2),solvejointvars))-sin(self.Tee[0])
                if self.CheckExpressionUnique(AllEquations,eq):
                    AllEquations.append(eq)
        
        # check if planar with respect to globalnormaldir
        extravar = None
        if globalnormaldir is not None:
            if Tallmult[0:3,0:3]*manipnormaldir == globalnormaldir:
                Tnormaltest = self.rodrigues(manipnormaldir,pi/2)
                # planar, so know that the sum of all hinge joints is equal to the final angle
                # can use this fact to substitute one angle with the other values
                angles = []
                isanglepositive = []
                for solvejoint in solvejointvars:
                    if self.IsHinge(solvejoint.name):
                        Tall0 = Tallmult[0:3,0:3].subs(solvejoint,S.Zero)
                        Tall1 = Tallmult[0:3,0:3].subs(solvejoint,pi/2)
                        if all([f==S.Zero for f in Tall0*Tnormaltest-Tall1]):
                            angles.append(solvejoint)
                            isanglepositive.append(True)
                        else:
                            angles.append(solvejoint)
                            isanglepositive.append(False)
                Tzero = Tallmult.subs([(a,S.Zero) for a in angles])
                for i in range(3):
                    if binormaldir[i].is_number:
                        binormaldir[i] = self.convertRealToRational(binormaldir[i])
                    if manipdir[i].is_number:
                        manipdir[i] = self.convertRealToRational(manipdir[i])
                zeroangle = atan2(binormaldir.dot(Tzero[0:3,0:3]*manipdir), globaldir.dot(Tzero[0:3,0:3]*manipdir))
                eqangles = self.Tee[0]-zeroangle
                for iangle, a in enumerate(angles[:-1]):
                    if isanglepositive[iangle]:
                        eqangles -= a
                    else:
                        eqangles += a
                if not isanglepositive[-1]:
                    eqangles = -eqangles
                extravar = (angles[-1],eqangles)
                coseq = cos(eqangles).expand(trig=True)
                sineq = sin(eqangles).expand(trig=True)
                AllEquationsOld = AllEquations
                AllEquations = [self.trigsimp(eq.subs([(cos(angles[-1]),coseq),(sin(angles[-1]),sineq)]).expand(),solvejointvars) for eq in AllEquationsOld]
                solvejointvarsold = list(solvejointvars)
                for var in solvejointvars:
                    if angles[-1].has(var):
                        solvejointvars.remove(var)
                        break

        self.sortComplexity(AllEquations)
        endbranchtree = [AST.SolverStoreSolution (jointvars,isHinge=[self.IsHinge(var.name) for var in jointvars])]
        if extravar is not None:
            solution = AST.SolverSolution(extravar[0].name, \
                                          jointeval = [extravar[1]], \
                                          isHinge = self.IsHinge(extravar[0].name))
            endbranchtree.insert(0,solution)
        
        try:
            tree = self.SolveAllEquations(AllEquations, \
                                          curvars = solvejointvars[:], \
                                          othersolvedvars = self.freejointvars, \
                                          solsubs = self.freevarsubs[:], \
                                          endbranchtree = endbranchtree)
            tree = self.verifyAllEquations(AllEquations,solvejointvars,self.freevarsubs,tree)
            
        except self.CannotSolveError, e:
            log.debug('failed to solve using SolveAllEquations: %s', e)
            if 0:
                solvejointvar0sols = solve(AllEquations[4], solvejointvars[0])
                NewEquations = [eq.subs(solvejointvars[0], solvejointvar0sols[0]) for eq in AllEquations]
                newsolution=AST.SolverSolution(solvejointvars[0].name, \
                                               jointeval = solvejointvar0sols, \
                                               isHinge = self.IsHinge(solvejointvars[0].name))
                endbranchtree.insert(0,newsolution)
                tree = self.SolveAllEquations(NewEquations, \
                                              curvars = solvejointvars[1:], \
                                              othersolvedvars = self.freejointvars, \
                                              solsubs = self.freevarsubs[:], \
                                              endbranchtree = endbranchtree)
            else:
                othersolvedvars = self.freejointvars[:]
                solsubs = self.freevarsubs[:]
                freevarinvsubs = [(f[1],f[0]) for f in self.freevarsubs]
                solinvsubs = [(f[1],f[0]) for f in solsubs]
                
                # single variable solutions
                solutions = []
                gatheredexceptions = []
                for curvar in solvejointvars:
                    othervars = [var for var in solvejointvars if var != curvar]
                    curvarsym = self.Variable(curvar)
                    raweqns = []
                    for e in AllEquations:
                        if (len(othervars) == 0 or not e.has(*othervars)) \
                           and e.has(curvar, curvarsym.htvar, curvarsym.cvar, curvarsym.svar):
                            eq = e.subs(self.freevarsubs+solsubs)
                            if self.CheckExpressionUnique(raweqns,eq):
                                raweqns.append(eq)
                    if len(raweqns) > 0:
                        try:
                            rawsolutions = self.solveSingleVariable(self.sortComplexity(raweqns), \
                                                                    curvar, \
                                                                    othersolvedvars, \
                                                                    unknownvars = solvejointvars)
                            for solution in rawsolutions:
                                self.ComputeSolutionComplexity(solution,othersolvedvars,solvejointvars)
                                solutions.append((solution,curvar))
                        except self.CannotSolveError, e:
                            gatheredexceptions.append((curvar.name, e))
                    else:
                        gatheredexceptions.append((curvar.name,None))
                if len(solutions) == 0:
                    raise self.CannotSolveError('failed to solve for equations. Possible errors are %s'%gatheredexceptions)
                
                firstsolution, firstvar = solutions[0]
                othersolvedvars.append(firstvar)
                solsubs += self.Variable(firstvar).subs
                curvars=solvejointvars[:]
                curvars.remove(firstvar)

                trigsubs = []
                polysubs = []
                polyvars = []
                for v in curvars:
                    if self.IsHinge(v.name):
                        var = self.Variable(v)
                        polysubs += [(cos(v),var.cvar),(sin(v),var.svar)]
                        polyvars += [var.cvar,var.svar]
                        trigsubs.append((var.svar**2,1-var.cvar**2))
                        trigsubs.append((var.svar**3,var.svar*(1-var.cvar**2)))
                    else:
                        polyvars.append(v)
                polysubsinv = [(b,a) for a,b in polysubs]
                rawpolyeqs = [Poly(Poly(eq.subs(polysubs),*polyvars).subs(trigsubs),*polyvars) \
                              for eq in AllEquations if eq.has(*curvars)]

                dummys = []
                dummysubs = []
                dummysubs2 = []
                dummyvars = []
                for i in range(0,len(polyvars),2):
                    dummy = Symbol('ht%s'%polyvars[i].name[1:])
                    # [0] - cos, [1] - sin
                    dummys.append(dummy)
                    dummysubs += [(polyvars[i],(1-dummy**2)/(1+dummy**2)),\
                                  (polyvars[i+1],2*dummy/(1+dummy**2))]
                    var = polyvars[i].subs(self.invsubs).args[0]
                    dummysubs2.append((var,2*atan(dummy)))
                    dummyvars.append((dummy,tan(0.5*var)))

                newreducedeqs = []
                for peq in rawpolyeqs:
                    maxdenom = [0]*(len(polyvars)/2)
                    for monoms in peq.monoms():
                        for i in range(len(maxdenom)):
                            maxdenom[i] = max(maxdenom[i],monoms[2*i]+monoms[2*i+1])
                    eqnew = S.Zero
                    for monoms,c in peq.terms():
                        term = c
                        for i in range(len(polyvars)):
                            num,denom = fraction(dummysubs[i][1])
                            term *= num**monoms[i]
                        # the denoms for 0,1 and 2,3 are the same
                        for i in range(len(maxdenom)):
                            denom = fraction(dummysubs[2*i][1])[1]
                            term *= denom**(maxdenom[i]-monoms[2*i]-monoms[2*i+1])
                        eqnew += term
                    newreducedeqs.append(Poly(eqnew,*dummys))

                newreducedeqs.sort(cmp=lambda x,y: len(x.monoms()) - len(y.monoms()))
                ileftvar = 0
                leftvar = dummys[ileftvar]
                exportcoeffeqs=None
                for ioffset in range(len(newreducedeqs)):
                    try:
                        exportcoeffeqs,exportmonoms = self.solveDialytically(newreducedeqs[ioffset:],ileftvar)
                        log.info('ioffset %d'%ioffset)
                        break
                    except self.CannotSolveError, e:
                        log.debug('solveDialytically errors: %s',e)

                if exportcoeffeqs is None:
                    raise self.CannotSolveError('failed to solveDialytically')

                coupledsolution = AST.SolverCoeffFunction(jointnames = [v.name for v in curvars], \
                                                          jointeval = [v[1] for v in dummysubs2], \
                                                          jointevalcos = [dummysubs[2*i][1] \
                                                                          for i in range(len(curvars))], \
                                                          jointevalsin = [dummysubs[2*i+1][1] \
                                                                          for i in range(len(curvars))], \
                                                          isHinges = [self.IsHinge(v.name) \
                                                                      for v in curvars], \
                                                          exportvar = [v.name for v in dummys], \
                                                          exportcoeffeqs = exportcoeffeqs, \
                                                          exportfnname = 'solvedialyticpoly12qep', \
                                                          rootmaxdim = 16)
                self.usinglapack = True
                tree = [firstsolution, coupledsolution]+ endbranchtree

        # package final solution
        chaintree = AST.SolverIKChainAxisAngle([(jointvars[ijoint],ijoint) \
                                                for ijoint in isolvejointvars], \
                                               [(v,i) for v,i in izip(self.freejointvars,self.ifreejointvars)], \
                                               Pee = self.Tee[0:3,3].subs(self.freevarsubs), \
                                               angleee = self.Tee[0,0].subs(self.freevarsubs), \
                                               jointtree = tree, \
                                               Pfk = self.Tfinal[0:3,3], \
                                               anglefk = self.Tfinal[0,0], \
                                               iktype = iktype)
        chaintree.dictequations += self.ppsubs
        return chaintree

    def buildEquationsFromTwoSides(self, leftside, rightside, usedvars, uselength = True):
        """

        uselength indicates whether to use the 2-norm of both sides.
        
        Called by 

        solveFullIK_Direction3D
        solveFullIK_Lookat3D
        solveFullIK_TranslationXY2D
        solveFullIK_TranslationXYOrientation3D
        solveFullIK_Ray4D
        solve5DIntersectingAxes
        buildEquationsFromPositions

        """
        
        # try to shift all the constants of each Position expression to one side
        for i in range(len(leftside)):
            for j in range(leftside[i].shape[0]):
                p   = leftside[i][j]
                pee = rightside[i][j]
                pconstterm   = None
                peeconstterm = None

                # pconstterm consists of all constants in p
                if p.is_Add:
                    pconstterm = [term for term in p.args if term.is_number]
                elif p.is_number:
                    pconstterm = [p]
                else:
                    continue

                # peeconstterm consists of all constants in pee
                if pee.is_Add:
                    peeconstterm = [term for term in pee.args if term.is_number]
                elif pee.is_number:
                    peeconstterm = [pee]
                else:
                    continue

                if len(pconstterm) > 0 and len(peeconstterm) > 0:
                    # shift it to the one that has the fewer constants
                    for term in peeconstterm if len(p.args) < len(pee.args) else pconstterm:
                        leftside[i][j]  -= term
                        rightside[i][j] -= term

        AllEquations = []
        self.gen_trigsubs(usedvars)
                        
        for i in range(len(leftside)):
            for j in range(leftside[i].shape[0]):
                eq = self.trigsimp_new(leftside[i][j]-rightside[i][j])
                # old function
                # e2 = self.trigsimp(leftside[i][j]-rightside[i][j], usedvars)
                # print "e  = ", e
                # print "e2 = ", e2
                # assert(e==e2)

                if self.codeComplexity(eq) < 1500:
                    eq = self.SimplifyTransform(eq)
                    
                if self.CheckExpressionUnique(AllEquations, eq):
                    AllEquations.append(eq)
                    
            if uselength:
                # here length means ||.||**2_2, square of the 2-norm
                p2  = S.Zero
                pe2 = S.Zero
                
                for j in range(leftside[i].shape[0]):
                    p2  += leftside[i][j]**2
                    pe2 += rightside[i][j]**2
                    
                if self.codeComplexity(p2) < 1200 and self.codeComplexity(pe2) < 1200:
                    # sympy's trigsimp/customtrigsimp give up too easily
                    eq = self.SimplifyTransform(self.trigsimp_new(p2)-self.trigsimp_new(pe2))

                    # if this length equation is not in our equation set, then add it into the set  
                    if self.CheckExpressionUnique(AllEquations, eq):
                        AllEquations.append(eq.expand())
                        
                else:
                    log.info('length of equation too big, skip %d, %d', \
                             self.codeComplexity(p2), self.codeComplexity(pe2))
                    
        self.sortComplexity(AllEquations)
        return AllEquations
        
    def buildEquationsFromPositions(self, T1links, T1linksinv, transvars, othersolvedvars, \
                                    uselength = True, \
                                    removesmallnumbers = True, \
                                    ignoreaxis = None):
        """
        Computes the product of all homogeneous matrices in T1links and that of matrices in T1linksinv, resp.

        Then constructs equations for their translation vectors, i.e. positions [0:3,3].

        uselength indicates whether to use the 2-norm of these vectors.

        ignoreaxis specifies the ONE axe that is ignored; it can be None, 0, 1, 2.
        """
        Taccum = eye(4)
        numvarsdone = 1
        Positions = []
        Positionsee = []
        indices = [0,1,2]

        # remove the ONE ignored axis; can be >1?
        if ignoreaxis is not None:
            indices.remove(ignoreaxis)
            
        for i in range(len(T1links)-1):
            Taccum = T1linksinv[i]*Taccum
            hasvars = [self.has(Taccum,v) for v in transvars]
            if __builtin__.sum(hasvars) == numvarsdone:
                # __builtin__.sum() is used to sum boolean vector
                Positions.append(Taccum.extract(indices,[3]))
                # TGN: this multiplyMatrix call can be improved in a cumulative manner
                # to do in future
                Positionsee.append(self.multiplyMatrix(T1links[(i+1):]).extract(indices,[3]))
                numvarsdone += 1
            if numvarsdone > 2:
                # more than 2 variables is almost always useless
                break

        if len(Positions) == 0:
            Positions.append(zeros((len(indices),1)))
            Positionsee.append(self.multiplyMatrix(T1links).extract(indices,[3]))

        # set constants below threshold to S.Zero
        if removesmallnumbers:
            for i in range(len(Positions)):
                for j in range(len(indices)):
                    Positions[i][j]   = self.RoundEquationTerms(Positions[i][j].expand())
                    Positionsee[i][j] = self.RoundEquationTerms(Positionsee[i][j].expand())
                    
        return self.buildEquationsFromTwoSides(Positions, Positionsee, \
                                               transvars+othersolvedvars, \
                                               uselength = uselength)

    def buildEquationsFromRotation(self,T0links,Ree,rotvars,othersolvedvars):
        """Ree is a 3x3 matrix
        """
        Raccum = Ree
        numvarsdone = 0
        AllEquations = []
        for i in range(len(T0links)):
            Raccum = T0links[i][0:3,0:3].transpose()*Raccum # transpose is the inverse 
            hasvars = [self.has(Raccum,v) for v in rotvars]
            if len(AllEquations) > 0 and __builtin__.sum(hasvars) >= len(rotvars):
                break
            if __builtin__.sum(hasvars) == numvarsdone:
                R = self.multiplyMatrix(T0links[(i+1):])
                Reqs = []
                for i in range(3):
                    
                    # TGN: ensure curvars is a subset of self.trigvars_subs
                    assert(len([z for z in othersolvedvars+rotvars if z in self.trigvars_subs]) == len(othersolvedvars+rotvars))
                    # equivalent?
                    assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars+rotvars]))
                    
                    Reqs.append([self.trigsimp_new(Raccum[i,j]-R[i,j]) for j in range(3)])
                for i in range(3):
                    for eq in Reqs[i]:
                        AllEquations.append(eq)
                numvarsdone += 1
                # take dot products (equations become unnecessarily complex)
#                 eqdots = [S.Zero, S.Zero, S.Zero]
#                 for i in range(3):
#                     eqdots[0] += Reqs[0][i] * Reqs[1][i]
#                     eqdots[1] += Reqs[1][i] * Reqs[2][i]
#                     eqdots[2] += Reqs[2][i] * Reqs[0][i]
#                 for i in range(3):
#                     AllEquations.append(self.trigsimp(eqdots[i].expand(),othersolvedvars+rotvars))
                #AllEquations.append((eqs[0]*eqs[0]+eqs[1]*eqs[1]+eqs[2]*eqs[2]-S.One).expand())
        self.sortComplexity(AllEquations)
        return AllEquations

    def buildRaghavanRothEquationsFromMatrix(self, T0, T1, solvejointvars, \
                                             simplify = True, \
                                             currentcasesubs = None):
        """Builds the 14 equations using only 5 unknowns. Method explained in [Raghavan1993]_. Basically take the position and one column/row so that the least number of variables are used.

        .. [Raghavan1993] M Raghavan and B Roth, "Inverse Kinematics of the General 6R Manipulator and related Linkages",  Journal of Mechanical Design, Volume 115, Issue 3, 1993.

        """
        p0 = T0[0:3,3]
        p1 = T1[0:3,3]
        p=p0-p1
        T = T0-T1
        numminvars = 100000
        for irow in range(3):
            hasvar = [self.has(T[0:3,irow],var) or self.has(p,var) for var in solvejointvars]
            numcurvars = __builtin__.sum(hasvar)
            if numminvars > numcurvars and numcurvars > 0:
                numminvars = numcurvars
                l0 = T0[0:3,irow]
                l1 = T1[0:3,irow]
            hasvar = [self.has(T[irow,0:3],var) or self.has(p,var) for var in solvejointvars]
            numcurvars = __builtin__.sum(hasvar)
            if numminvars > numcurvars and numcurvars > 0:
                numminvars = numcurvars
                l0 = T0[irow,0:3].transpose()
                l1 = T1[irow,0:3].transpose()
        if currentcasesubs is not None:
            p0 = p0.subs(currentcasesubs)
            p1 = p1.subs(currentcasesubs)
            l0 = l0.subs(currentcasesubs)
            l1 = l1.subs(currentcasesubs)
        return self.buildRaghavanRothEquations(p0,p1,l0,l1,solvejointvars,simplify,currentcasesubs),numminvars

    def CheckEquationForVarying(self, eq):
        return eq.has('vj0px') or eq.has('vj0py') or eq.has('vj0pz')
    
    def buildRaghavanRothEquationsOld(self,p0,p1,l0,l1,solvejointvars):
        eqs = []
        for i in range(3):
            eqs.append([l0[i],l1[i]])
        for i in range(3):
            eqs.append([p0[i],p1[i]])
        l0xp0 = l0.cross(p0)
        l1xp1 = l1.cross(p1)
        for i in range(3):
            eqs.append([l0xp0[i],l1xp1[i]])
        ppl0 = p0.dot(p0)*l0 - 2*l0.dot(p0)*p0
        ppl1 = p1.dot(p1)*l1 - 2*l1.dot(p1)*p1
        for i in range(3):
            eqs.append([ppl0[i],ppl1[i]])
        eqs.append([p0.dot(p0),p1.dot(p1)])
        eqs.append([l0.dot(p0),l1.dot(p1)])
        # prune any that have varying symbols
        eqs = [(eq0,eq1) for eq0,eq1 in eqs \
               if not self.CheckEquationForVarying(eq0) \
               and not self.CheckEquationForVarying(eq1)]
        trigsubs = []
        polysubs = []
        polyvars = []
        for v in solvejointvars:
            self._CheckPreemptFn(progress = 0.05)
            polyvars.append(v)
            if self.IsHinge(v.name):
                var = self.Variable(v)
                polysubs += [(cos(v),var.cvar),(sin(v),var.svar)]
                polyvars += [var.cvar,var.svar]
                trigsubs.append((var.svar**2,1-var.cvar**2))
                trigsubs.append((var.svar**3,var.svar*(1-var.cvar**2)))
        for v in self.freejointvars:
            if self.IsHinge(v.name):
                trigsubs.append((sin(v)**2,1-cos(v)**2))
                trigsubs.append((sin(v)**3,sin(v)*(1-cos(v)**2)))
        polysubsinv = [(b,a) for a,b in polysubs]
        usedvars = []
        for j in range(2):
            usedvars.append([var for var in polyvars \
                             if any([eq[j].subs(polysubs).has(var) for eq in eqs])])
        polyeqs = []
        for i in range(len(eqs)):
            polyeqs.append([None,None])        
        for j in range(2):
            self._CheckPreemptFn(progress = 0.05)
            for i in range(len(eqs)):
                poly0 = Poly(eqs[i][j].subs(polysubs),*usedvars[j]).subs(trigsubs)
                poly1 = Poly(poly0.expand().subs(trigsubs),*usedvars[j])
                if poly1 == S.Zero:
                    polyeqs[i][j] = poly1
                else:
                    polyeqs[i][j] = self.SimplifyTransformPoly(poly1)
        # remove all fractions? having big integers could blow things up...
        return polyeqs
    
    def buildRaghavanRothEquations(self,p0, p1, l0, l1, solvejointvars, \
                                   simplify = True, currentcasesubs = None):
        trigsubs = []
        polysubs = []
        polyvars = []
        for v in solvejointvars:
            polyvars.append(v)
            if self.IsHinge(v.name):
                var = self.Variable(v)
                polysubs += [(cos(v),var.cvar),(sin(v),var.svar)]
                polyvars += [var.cvar,var.svar]
                trigsubs.append((var.svar**2,1-var.cvar**2))
                trigsubs.append((var.svar**3,var.svar*(1-var.cvar**2)))
        for v in self.freejointvars:
            if self.IsHinge(v.name):
                trigsubs.append((sin(v)**2,1-cos(v)**2))
                trigsubs.append((sin(v)**3,sin(v)*(1-cos(v)**2)))
        if currentcasesubs is not None:
            trigsubs += currentcasesubs
        polysubsinv = [(b,a) for a,b in polysubs]
        polyeqs = []
        for i in range(14):
            polyeqs.append([None,None])
            
        eqs = []
        for i in range(3):
            eqs.append([l0[i],l1[i]])
        for i in range(3):
            eqs.append([p0[i],p1[i]])
        l0xp0 = l0.cross(p0)
        l1xp1 = l1.cross(p1)
        for i in range(3):
            eqs.append([l0xp0[i],l1xp1[i]])
        eqs.append([p0.dot(p0),p1.dot(p1)])
        eqs.append([l0.dot(p0),l1.dot(p1)])
        starttime = time.time()
        usedvars = []
        for j in range(2):
            usedvars.append([var for var in polyvars if any([eq[j].subs(polysubs).has(var) for eq in eqs])])
        for i in range(len(eqs)):
            self._CheckPreemptFn(progress = 0.05)
            if not self.CheckEquationForVarying(eqs[i][0]) and not self.CheckEquationForVarying(eqs[i][1]):
                for j in range(2):
                    if polyeqs[i][j] is not None:
                        continue
                    poly0 = Poly(eqs[i][j].subs(polysubs),*usedvars[j]).subs(trigsubs)
                    if self.codeComplexity(poly0.as_expr()) < 5000:
                        poly1 = Poly(poly0.expand().subs(trigsubs),*usedvars[j])
                        if not simplify or poly1 == S.Zero:
                            polyeqs[i][j] = poly1
                        else:
                            polyeqs[i][j] = self.SimplifyTransformPoly(poly1)
                    else:
                        polyeqs[i][j] = Poly(poly0.expand().subs(trigsubs),*usedvars[j])
        #ppl0 = p0.dot(p0)*l0 - 2*l0.dot(p0)*p0
        #ppl1 = p1.dot(p1)*l1 - 2*l1.dot(p1)*p1        
        ppl0 = polyeqs[9][0].as_expr()*l0 - 2*polyeqs[10][0].as_expr()*p0 # p0.dot(p0)*l0 - 2*l0.dot(p0)*p0
        ppl1 = polyeqs[9][1].as_expr()*l1 - 2*polyeqs[10][1].as_expr()*p1 # p1.dot(p1)*l1 - 2*l1.dot(p1)*p1
        for i in range(3):
            eqs.append([ppl0[i],ppl1[i]])
        for i in range(11, len(eqs)):
            if not self.CheckEquationForVarying(eqs[i][0]) and not self.CheckEquationForVarying(eqs[i][1]):
                for j in range(2):
                    if polyeqs[i][j] is not None:
                        continue
                    poly0 = Poly(eqs[i][j].subs(polysubs),*usedvars[j]).subs(trigsubs)
                    if self.codeComplexity(poly0.as_expr()) < 5000:
                        poly1 = Poly(poly0.expand().subs(trigsubs),*usedvars[j])
                        if not simplify or poly1 == S.Zero:
                            polyeqs[i][j] = poly1
                        else:
                            polyeqs[i][j] = self.SimplifyTransformPoly(poly1)
                    else:
                        log.warn('raghavan roth equation (%d,%d) too complex', i, j)
                        polyeqs[i][j] = Poly(poly0.expand().subs(trigsubs),*usedvars[j])
        log.info('computed in %fs', time.time()-starttime)
        # prune any that have varying symbols
        # remove all fractions? having big integers could blow things up...
        return [[peq0, peq1] for peq0, peq1 in polyeqs if peq0 is not None and peq1 is not None and not self.CheckEquationForVarying(peq0) and not self.CheckEquationForVarying(peq1)]

    def reduceBothSides(self,polyeqs):
        """Reduces a set of equations in 5 unknowns to a set of equations with 3 unknowns by solving for one side with respect to another.
        The input is usually the output of buildRaghavanRothEquations.
        """
        usedvars = [polyeqs[0][0].gens, polyeqs[0][1].gens]
        reducedelayed = []
        for j in range(2):
            if len(usedvars[j]) <= 4:
                leftsideeqs = [polyeq[j] for polyeq in polyeqs if sum(polyeq[j].degree_list()) > 0]
                rightsideeqs = [polyeq[1-j] for polyeq in polyeqs if sum(polyeq[j].degree_list()) > 0]
                if all([all(d <= 2 for d in eq.degree_list()) for eq in leftsideeqs]):
                    try:
                        numsymbolcoeffs, _computereducedequations = self.reduceBothSidesSymbolicallyDelayed(leftsideeqs,rightsideeqs)
                        reducedelayed.append([j,leftsideeqs,rightsideeqs,__builtin__.sum(numsymbolcoeffs), _computereducedequations])
                    except self.CannotSolveError:
                        continue
        
        # sort with respect to least number of symbols
        reducedelayed.sort(lambda x,y: x[3]-y[3])

        reducedeqs = []
        tree = []
        for j,leftsideeqs,rightsideeqs,numsymbolcoeffs, _computereducedequations in reducedelayed:
            self._CheckPreemptFn(progress = 0.06)
            try:
                reducedeqs2 = _computereducedequations()
                if len(reducedeqs2) == 0:
                    log.info('forcing matrix inverse (might take some time)')
                    reducedeqs2,tree = self.reduceBothSidesInverseMatrix(leftsideeqs,rightsideeqs)
                if len(reducedeqs2) > 0:
                    # success, add all the reduced equations
                    reducedeqs += [[Poly(eq[0],*usedvars[j]),Poly(eq[1],*usedvars[1-j])] for eq in reducedeqs2] + [[Poly(S.Zero,*polyeq[j].gens),polyeq[1-j]-polyeq[j].as_expr()] for polyeq in polyeqs if sum(polyeq[j].degree_list()) == 0]
                    if len(reducedeqs) > 0:
                        break;
            except self.CannotSolveError,e:
                log.warn(e)
                continue

        if len(reducedeqs) > 0:
            # check if any substitutions are needed
#             for eq in reducedeqs:
#                 for j in range(2):
#                     eq[j] = Poly(eq[j].subs(trigsubs).as_expr().expand(),*eq[j].gens)
            polyeqs = reducedeqs
        return [eq for eq in polyeqs if eq[0] != S.Zero or eq[1] != S.Zero],tree

    def reduceBothSidesInverseMatrix(self,leftsideeqs,rightsideeqs):
        """solve a linear system inside the program since the matrix cannot be reduced so easily
        """
        allmonomsleft = set()
        for peq in leftsideeqs:
            allmonomsleft = allmonomsleft.union(set(peq.monoms()))
        allmonomsleft = list(allmonomsleft)
        allmonomsleft.sort()
        if __builtin__.sum(allmonomsleft[0]) == 0:
            allmonomsleft.pop(0)
        if len(leftsideeqs) < len(allmonomsleft):
            raise self.CannotSolveError('left side has too few equations for the number of variables %d<%d'%(len(leftsideeqs),len(allmonomsleft)))
        
        systemcoeffs = []
        for ileft,left in enumerate(leftsideeqs):
            coeffs = [S.Zero]*len(allmonomsleft)
            rank = 0
            for m,c in left.terms():
                if __builtin__.sum(m) > 0:
                    if c != S.Zero:
                        rank += 1
                    coeffs[allmonomsleft.index(m)] = c
            systemcoeffs.append((rank,ileft,coeffs))
        # ideally we want to try all combinations of simple equations first until we arrive to linearly independent ones.
        # However, in practice most of the first equations are linearly dependent and it takes a lot of time to prune all of them,
        # so start at the most complex
        systemcoeffs.sort(lambda x,y: -x[0]+y[0])
        # sort left and right in the same way
        leftsideeqs = [leftsideeqs[ileft] for rank,ileft,coeffs in systemcoeffs]
        rightsideeqs = [rightsideeqs[ileft] for rank,ileft,coeffs in systemcoeffs]

        A = zeros((len(allmonomsleft),len(allmonomsleft)))
        Asymbols = []
        for i in range(A.shape[0]):
            Asymbols.append([Symbol('gconst%d_%d'%(i,j)) for j in range(A.shape[1])])
        solution = None
        for eqindices in combinations(range(len(leftsideeqs)),len(allmonomsleft)):
            self._CheckPreemptFn(progress = 0.06)
            for i,index in enumerate(eqindices):
                for k in range(len(allmonomsleft)):
                    A[i,k] = systemcoeffs[index][2][k]
            nummatrixsymbols = __builtin__.sum([1 for a in A if not a.is_number])
            if nummatrixsymbols > 10:
                # if too many symbols, evaluate numerically
                if not self.IsDeterminantNonZeroByEval(A, evalfirst=nummatrixsymbols>60): # pi_robot has 55 symbols and still finishes ok
                    continue
                log.info('found non-zero determinant by evaluation')
            else:
                det = self.det_bareis(A,*self.pvars)
                if det == S.Zero:
                    continue
                solution.checkforzeros = [self.removecommonexprs(det,onlygcd=False,onlynumbers=True)]
            solution = AST.SolverMatrixInverse(A=A,Asymbols=Asymbols)
            self.usinglapack = True
            Aadj=A.adjugate() # too big to be useful for now, but can be used to see if any symbols are always 0
            break
        if solution is None:
            raise self.CannotSolveError('failed to find %d linearly independent equations'%len(allmonomsleft))
        
        reducedeqs = []
        for i in range(len(allmonomsleft)):
            var=S.One
            for k,kpower in enumerate(allmonomsleft[i]):
                if kpower != 0:
                    var *= leftsideeqs[0].gens[k]**kpower
            pright = S.Zero
            for k in range(len(allmonomsleft)):
                if Aadj[i,k] != S.Zero:
                    pright += Asymbols[i][k] * (rightsideeqs[eqindices[k]].as_expr()-leftsideeqs[eqindices[k]].TC())
            reducedeqs.append([var,pright.expand()])
        othereqindices = set(range(len(leftsideeqs))).difference(set(eqindices))
        for i in othereqindices:
            # have to multiply just the constant by the determinant
            neweq = rightsideeqs[i].as_expr()
            for m,c in leftsideeqs[i].terms():
                if __builtin__.sum(m) > 0:
                    neweq -= c*reducedeqs[allmonomsleft.index(m)][1]
                else:
                    neweq -= c
            reducedeqs.append([S.Zero,neweq])
        return reducedeqs, [solution]

#                 Adj=M[:,:-1].adjugate()
#                 #D=M[:,:-1].det()
#                 D=M[:,:-1].det()
#                 sols=-Adj*M[:,-1]
#                 solsubs = []
#                 for i,v in enumerate(newunknowns):
#                     newsol=sols[i].subs(localsymbols)
#                     solsubs.append((v,newsol))
#                     reducedeqs.append([v.subs(localsymbols)*D,newsol])
#                 othereqindices = set(range(len(newleftsideeqs))).difference(set(eqindices))
#                 for i in othereqindices:
#                     # have to multiply just the constant by the determinant
#                     newpoly = S.Zero
#                     for c,m in newleftsideeqs[i].terms():
#                         monomindices = [index for index in range(len(newunknowns)) if m[index]>0]
#                         if len(monomindices) == 0:
#                             newpoly += c.subs(localsymbols)*D
#                         else:
#                             assert(len(monomindices)==1)
#                             newpoly += c.subs(localsymbols)*solsubs[monomindices[0]][1]
#                     reducedeqs.append([S.Zero,newpoly])
#                 break

#                 # there are too many symbols, so have to resolve to a little more involved method
#                 P,L,DD,U= M[:,:-1].LUdecompositionFF(*self.pvars)
#                 finalnums = S.One
#                 finaldenoms = S.One
#                 for i in range(len(newunknowns)):
#                     n,d = self.recursiveFraction(L[i,i]*U[i,i]/DD[i,i])
#                     finalnums *= n
#                     finaldenoms *= d
#                     n,d = self.recursiveFraction(DD[i,i])
#                     q,r = div(n,d,*pvars)
#                     DD[i,i] = q
#                     assert(r==S.Zero)
#                 det,r = div(finalnums,finaldenoms,*pvars)
#                 assert(r==S.Zero)
#                 b = -P*M[:,-1]
#                 y = [[b[0],L[0,0]]]
#                 for i in range(1,L.shape[0]):
#                     commondenom=y[0][1]
#                     for j in range(1,i):
#                         commondenom=lcm(commondenom,y[j][1],*pvars)
#                     accum = S.Zero
#                     for j in range(i):
#                         accum += L[i,j]*y[j][0]*(commondenom/y[j][1])
#                     res = (commondenom*b[i]-accum)/(commondenom*L[i,i])
#                     y.append(self.recursiveFraction(res))
# 
#                 ynew = []
#                 for i in range(L.shape[0]):
#                     q,r=div(y[i][0]*DD[i,i],y[i][1],*pvars)
#                     print 'remainder: ',r
#                     ynew.append(q)
#                 
#                 x = [[ynew[-1],U[-1,-1]]]
#                 for i in range(U.shape[0]-2,-1,-1):
#                     commondenom=x[0][1]
#                     for j in range(i+1,U.shape[0]):
#                         commondenom=lcm(commondenom,x[j][1],*pvars)
#                     accum = S.Zero
#                     for j in range(i+1,U.shape[0]):
#                         accum += U[i,j]*x[j][0]*(commondenom/x[j][1])
#                     res = (commondenom*b[i]-accum)/(commondenom*U[i,i])
#                     x.append(self.recursiveFraction(res))
#                 
#                 print 'ignoring num symbols: ',numsymbols
#                 continue

    def reduceBothSidesSymbolically(self,*args,**kwargs):
        numsymbolcoeffs, _computereducedequations = self.reduceBothSidesSymbolicallyDelayed(*args,**kwargs)
        return _computereducedequations()

    def reduceBothSidesSymbolicallyDelayed(self,leftsideeqs,rightsideeqs,maxsymbols=10,usesymbols=True):
        """the left and right side of the equations need to have different variables
        """
        assert(len(leftsideeqs)==len(rightsideeqs))
        # first count the number of different monomials, then try to solve for each of them
        symbolgen = cse_main.numbered_symbols('const')
        vargen = cse_main.numbered_symbols('tempvar')
        rightsidedummy = []
        localsymbols = []
        dividesymbols = []
        allmonoms = dict()
        for left,right in izip(leftsideeqs,rightsideeqs):
            if right != S.Zero:
                rightsidedummy.append(symbolgen.next())
                localsymbols.append((rightsidedummy[-1],right.as_expr().expand()))
            else:
                rightsidedummy.append(S.Zero)
            for m in left.monoms():
                if __builtin__.sum(m) > 0 and not m in allmonoms:
                    newvar = vargen.next()
                    localsymbols.append((newvar,Poly.from_dict({m:S.One},*left.gens).as_expr()))
                    allmonoms[m] = newvar

        if len(leftsideeqs) < len(allmonoms):
            raise self.CannotSolveError('left side has too few equations for the number of variables %d<%d'%(len(leftsideeqs),len(allmonoms)))
        
        if len(allmonoms) == 0:
            def _returnequations():
                return [[left,right] for left,right in izip(leftsideeqs,rightsideeqs)]
            
            return 0, _returnequations
        
        unknownvars = leftsideeqs[0].gens
        newleftsideeqs = []
        numsymbolcoeffs = []
        for left,right in izip(leftsideeqs,rightsidedummy):
            left = left - right
            newleft = Poly(S.Zero,*allmonoms.values())
            leftcoeffs = [c for m,c in left.terms() if __builtin__.sum(m) > 0]
            allnumbers = all([c.is_number for c in leftcoeffs])
            if usesymbols and not allnumbers:
                # check if all the equations are within a constant from each other
                # This is neceesary since the current linear system solver cannot handle too many symbols.
                reducedeq0,common0 = self.removecommonexprs(leftcoeffs[0],returncommon=True)
                commonmults = [S.One]
                for c in leftcoeffs[1:]:
                    reducedeq1,common1 = self.removecommonexprs(c,returncommon=True)
                    if self.equal(reducedeq1,reducedeq0):
                        commonmults.append(common1/common0)
                    elif self.equal(reducedeq1,-reducedeq0):
                        commonmults.append(-common1/common0)
                    else:
                        break
                if len(commonmults) == len(leftcoeffs):
                    # divide everything by reducedeq0
                    index = 0
                    for m,c in left.terms():
                        if __builtin__.sum(m) > 0:
                            newleft = newleft + commonmults[index]*allmonoms.get(m)
                            index += 1
                        else:
                            # look in the dividesymbols for something similar
                            gmult = None
                            for gsym,geq in dividesymbols:
                                greducedeq,gcommon = self.removecommonexprs(S.One/geq,returncommon=True)
                                if self.equal(greducedeq,reducedeq0):
                                    gmult = gsym*(gcommon/common0)
                                    break
                                elif self.equal(greducedeq,-reducedeq0):
                                    gmult = gsym*(-gcommon/common0)
                                    break
                            if gmult is None:
                                gmult = symbolgen.next()
                                dividesymbols.append((gmult,S.One/leftcoeffs[0]))
                            newc = (c*gmult).subs(localsymbols).expand()
                            sym = symbolgen.next()
                            localsymbols.append((sym,newc))
                            newleft = newleft + sym
                    numsymbolcoeffs.append(0)
                    newleftsideeqs.append(newleft)
                    continue
            numsymbols = 0
            for m,c in left.terms():
                polyvar = S.One
                if __builtin__.sum(m) > 0:
                    polyvar = allmonoms.get(m)
                    if not c.is_number:
                        numsymbols += 1
                newleft = newleft + c*polyvar
            numsymbolcoeffs.append(numsymbols)
            newleftsideeqs.append(newleft)

        def _computereducedequations():
            reducedeqs = []
            # order the equations based on the number of terms
            newleftsideeqs.sort(lambda x,y: len(x.monoms()) - len(y.monoms()))
            newunknowns = newleftsideeqs[0].gens
            log.info('solving for all pairwise variables in %s, number of symbol coeffs are %s',unknownvars,__builtin__.sum(numsymbolcoeffs))
            systemcoeffs = []
            for eq in newleftsideeqs:
                eqdict = eq.as_dict()
                coeffs = []
                for i,var in enumerate(newunknowns):
                    monom = [0]*len(newunknowns)
                    monom[i] = 1
                    coeffs.append(eqdict.get(tuple(monom),S.Zero))
                monom = [0]*len(newunknowns)
                coeffs.append(-eqdict.get(tuple(monom),S.Zero))
                systemcoeffs.append(coeffs)
            
            detvars = [s for s,v in localsymbols] + self.pvars
            for eqindices in combinations(range(len(newleftsideeqs)),len(newunknowns)):
                # very quick rejection
                numsymbols = __builtin__.sum([numsymbolcoeffs[i] for i in eqindices])
                if numsymbols > maxsymbols:
                    continue
                M = Matrix([systemcoeffs[i] for i in eqindices])
                det = self.det_bareis(M[:,:-1], *detvars)
                if det == S.Zero:
                    continue
                try:
                    eqused = [newleftsideeqs[i] for i in eqindices]
                    solution=solve(eqused,newunknowns)
                except IndexError:
                    # not enough equations?
                    continue                
                if solution is not None and all([self.isValidSolution(value.subs(localsymbols)) for key,value in solution.iteritems()]):
                    # substitute 
                    solsubs = []
                    allvalid = True
                    for key,value in solution.iteritems():
                        valuesub = value.subs(localsymbols)
                        solsubs.append((key,valuesub))
                        reducedeqs.append([key.subs(localsymbols),valuesub])
                    othereqindices = set(range(len(newleftsideeqs))).difference(set(eqindices))
                    for i in othereqindices:
                        reducedeqs.append([S.Zero,(newleftsideeqs[i].subs(solsubs).subs(localsymbols)).as_expr().expand()])
                    break
            
            # remove the dividesymbols from reducedeqs
            for sym,ivalue in dividesymbols:
                value=1/ivalue
                for i in range(len(reducedeqs)):
                    eq = reducedeqs[i][1]
                    if eq.has(sym):
                        neweq = S.Zero
                        peq = Poly(eq,sym)
                        for m,c in peq.terms():
                            neweq += c*value**(peq.degree(0) - m[0])
                        reducedeqs[i][1] = neweq.expand()
                        reducedeqs[i][0] = (reducedeqs[i][0]*value**peq.degree(0)).expand()
            if len(reducedeqs) > 0:
                log.info('finished with %d equations',len(reducedeqs))
            return reducedeqs
        
        return numsymbolcoeffs, _computereducedequations
    
    def solveManochaCanny(self,rawpolyeqs,solvejointvars,endbranchtree, AllEquationsExtra=None, currentcases=None, currentcasesubs=None):
        """Solves the IK equations using eigenvalues/eigenvectors of a 12x12 quadratic eigenvalue problem. Method explained in
        
        Dinesh Manocha and J.F. Canny. "Efficient inverse kinematics for general 6R manipulators", IEEE Transactions on Robotics and Automation, Volume 10, Issue 5, Oct 1994.
        """
        log.info('attempting manocha/canny general ik method')
        PolyEquations, raghavansolutiontree = self.reduceBothSides(rawpolyeqs)
        # find all equations with zeros on the left side
        RightEquations = []
        for ipeq,peq in enumerate(PolyEquations):
            if peq[0] == S.Zero:
                if len(raghavansolutiontree) > 0 or peq[1] == S.Zero:
                    # give up on optimization
                    RightEquations.append(peq[1])
                else:
                    RightEquations.append(self.SimplifyTransformPoly(peq[1]))
        
        if len(RightEquations) < 6:
            raise self.CannotSolveError('number of equations %d less than 6'%(len(RightEquations)))
        
        # sort with respect to the number of monomials
        RightEquations.sort(lambda x, y: len(x.monoms())-len(y.monoms()))
        
        # substitute with dummy=tan(half angle)
        symbols = RightEquations[0].gens
        symbolsubs = [(symbols[i].subs(self.invsubs),symbols[i]) for i in range(len(symbols))]
        unsolvedsymbols = []
        for solvejointvar in solvejointvars:
            testvars = self.Variable(solvejointvar).vars
            if not any([v in symbols for v in testvars]):
                unsolvedsymbols += testvars

        # check that the coefficients of the reduced equations do not contain any unsolved variables
        for peq in RightEquations:
            if peq.has(*unsolvedsymbols):
                raise self.CannotSolveError('found unsolved symbol being used so ignoring: %s'%peq)
        
        log.info('solving simultaneously for symbols: %s',symbols)

        dummys = []
        dummysubs = []
        dummysubs2 = []
        dummyvars = []
        usedvars = []
        singlevariables = []
        i = 0
        while i < len(symbols):
            dummy = Symbol('ht%s'%symbols[i].name[1:])
            var = symbols[i].subs(self.invsubs)
            if not isinstance(var,Symbol):
                # [0] - cos, [1] - sin
                var = var.args[0]
                dummys.append(dummy)
                dummysubs += [(symbols[i],(1-dummy**2)/(1+dummy**2)),(symbols[i+1],2*dummy/(1+dummy**2))]
                dummysubs2.append((var,2*atan(dummy)))
                dummyvars.append((dummy,tan(0.5*var)))
                if not var in usedvars:
                    usedvars.append(var)
                i += 2
            else:
                singlevariables.append(var)
                # most likely a single variable
                dummys.append(var)
                dummysubs += [(var,var)]
                dummysubs2.append((var,var))
                if not var in usedvars:
                    usedvars.append(var)                    
                i += 1

        newreducedeqs = []
        for peq in RightEquations:
            maxdenom = dict()
            for monoms in peq.monoms():
                i = 0
                while i < len(monoms):
                    if peq.gens[i].name[0] == 'j':
                        # single variable
                        maxdenom[peq.gens[i]] = max(maxdenom.get(peq.gens[i],0),monoms[i])
                        i += 1
                    else:
                        maxdenom[peq.gens[i]] = max(maxdenom.get(peq.gens[i],0),monoms[i]+monoms[i+1])
                        i += 2
            eqnew = S.Zero
            for monoms,c in peq.terms():
                term = c
                for i in range(len(dummysubs)):
                    num,denom = fraction(dummysubs[i][1])
                    term *= num**monoms[i]
                # the denoms for 0,1 and 2,3 are the same
                i = 0
                while i < len(monoms):
                    if peq.gens[i].name[0] == 'j':
                        denom = fraction(dummysubs[i][1])[1]
                        term *= denom**(maxdenom[peq.gens[i]]-monoms[i])
                        i += 1
                    else:
                        denom = fraction(dummysubs[i][1])[1]
                        term *= denom**(maxdenom[peq.gens[i]]-monoms[i]-monoms[i+1])
                        i += 2
                eqnew += term
            newreducedeqs.append(Poly(eqnew,*dummys))
            
        # check for equations with a single variable
        if len(singlevariables) > 0:
            try:
                AllEquations = [eq.subs(self.invsubs).as_expr() for eq in newreducedeqs]
                tree = self.SolveAllEquations(AllEquations,curvars=dummys,othersolvedvars=[],solsubs=self.freevarsubs,endbranchtree=endbranchtree, currentcases=currentcases, currentcasesubs=currentcasesubs)
                return raghavansolutiontree+tree,usedvars
            except self.CannotSolveError:
                pass

            if 0:
                # try solving for the single variable and substituting for the rest of the equations in order to get a set of equations without the single variable
                var = singlevariables[0]
                monomindex = symbols.index(var)
                singledegreeeqs = []
                AllEquations = []
                for peq in newreducedeqs:
                    if all([m[monomindex] <= 1 for m in peq.monoms()]):
                        newpeq = Poly(peq,var)
                        if sum(newpeq.degree_list()) > 0:
                            singledegreeeqs.append(newpeq)
                        else:
                            AllEquations.append(peq.subs(self.invsubs).as_expr())
                for peq0, peq1 in combinations(singledegreeeqs,2):
                    AllEquations.append(simplify((peq0.TC()*peq1.LC() - peq0.LC()*peq1.TC()).subs(self.invsubs)))

                log.info(str(AllEquations))
                #sol=self.SolvePairVariablesHalfAngle(AllEquations,usedvars[1],usedvars[2],[])

        # choose which leftvar can determine the singularity of the following equations!
        exportcoeffeqs = None
        getsubs = raghavansolutiontree[0].getsubs if len(raghavansolutiontree) > 0 else None
        for ileftvar in range(len(dummys)):
            leftvar = dummys[ileftvar]
            try:
                exportcoeffeqs,exportmonoms = self.solveDialytically(newreducedeqs,ileftvar,getsubs=getsubs)
                break
            except self.CannotSolveError,e:
                log.warn('failed with leftvar %s: %s',leftvar,e)

        if exportcoeffeqs is None:
            raise self.CannotSolveError('failed to solve dialytically')
        if ileftvar > 0:
            raise self.CannotSolveError('solving equations dialytically succeeded with var index %d, unfortunately code generation supports only index 0'%ileftvar)
    
        jointevalcos=[d[1] for d in dummysubs if d[0].name[0] == 'c']
        jointevalsin=[d[1] for d in dummysubs if d[0].name[0] == 's']
        #jointeval=[d[1] for d in dummysubs if d[0].name[0] == 'j']
        coupledsolution = AST.SolverCoeffFunction(jointnames=[v.name for v in usedvars],jointeval=[v[1] for v in dummysubs2],jointevalcos=jointevalcos, jointevalsin=jointevalsin, isHinges=[self.IsHinge(v.name) for v in usedvars],exportvar=[v.name for v in dummys],exportcoeffeqs=exportcoeffeqs,exportfnname='solvedialyticpoly12qep',rootmaxdim=16)
        self.usinglapack = True
        return raghavansolutiontree+[coupledsolution]+endbranchtree,usedvars

    def solveLiWoernleHiller(self,rawpolyeqs,solvejointvars,endbranchtree,AllEquationsExtra=[], currentcases=None, currentcasesubs=None):
        """Li-Woernle-Hiller procedure covered in 
        Jorge Angeles, "Fundamentals of Robotics Mechanical Systems", Springer, 2007.
        """
        log.info('attempting li/woernle/hiller general ik method')
        if len(rawpolyeqs[0][0].gens) <len(rawpolyeqs[0][1].gens):
            for peq in rawpolyeqs:
                peq[0],peq[1] = peq[1],peq[0]
        
        originalsymbols = list(rawpolyeqs[0][0].gens)
        symbolsubs = [(originalsymbols[i].subs(self.invsubs),originalsymbols[i]) for i in range(len(originalsymbols))]
        numsymbols = 0
        for solvejointvar in solvejointvars:
            for var in self.Variable(solvejointvar).vars:
                if var in originalsymbols:
                    numsymbols += 1
                    break
        if numsymbols != 3:
            raise self.CannotSolveError('Li/Woernle/Hiller method requires 3 unknown variables, has %d'%numsymbols)
        
        if len(originalsymbols) != 6:
            log.warn('symbols %r are not all rotational, is this really necessary?'%originalsymbols)
            raise self.CannotSolveError('symbols %r are not all rotational, is this really necessary?'%originalsymbols)
            
        # choose which leftvar can determine the singularity of the following equations!
        allowedindices = []
        for i in range(len(originalsymbols)):
            # if first symbol is cjX, then next should be sjX
            if originalsymbols[i].name[0] == 'c':
                assert( originalsymbols[i+1].name == 's'+originalsymbols[i].name[1:])
                if 8 == __builtin__.sum([int(peq[0].has(originalsymbols[i],originalsymbols[i+1])) for peq in rawpolyeqs]):
                    allowedindices.append(i)
        if len(allowedindices) == 0:
            log.warn('could not find any variable where number of equations is exacty 8, trying all possibilities')
            for i in range(len(originalsymbols)):
                # if first symbol is cjX, then next should be sjX
                if originalsymbols[i].name[0] == 'c':
                    assert( originalsymbols[i+1].name == 's'+originalsymbols[i].name[1:])
                    allowedindices.append(i)
            #raise self.CannotSolveError('need exactly 8 equations of one variable')
        log.info('allowed indices: %s', allowedindices)
        for allowedindex in allowedindices:
            solutiontree = []
            checkforzeros = []
            symbols = list(originalsymbols)
            cvar = symbols[allowedindex]
            svar = symbols[allowedindex+1]
            varname = cvar.name[1:]
            tvar = Symbol('ht'+varname)
            symbols.remove(cvar)
            symbols.remove(svar)
            symbols.append(tvar)
            othersymbols = list(rawpolyeqs[0][1].gens)
            othersymbols.append(tvar)
            polyeqs = [[peq[0].as_expr(),peq[1]] for peq in rawpolyeqs if peq[0].has(cvar,svar)]
            neweqs=[]
            unusedindices = set(range(len(polyeqs)))
            for i in range(len(polyeqs)):
                if not i in unusedindices:
                    continue
                p0 = Poly(polyeqs[i][0],cvar,svar)
                p0dict=p0.as_dict()
                for j in unusedindices:
                    if j == i:
                        continue
                    p1 = Poly(polyeqs[j][0],cvar,svar) # TODO can be too complex
                    p1dict=p1.as_dict()
                    r0 = polyeqs[i][1].as_expr()
                    r1 = polyeqs[j][1].as_expr()
                    if self.equal(p0dict.get((1,0),S.Zero),-p1dict.get((0,1),S.Zero)) and self.equal(p0dict.get((0,1),S.Zero),p1dict.get((1,0),S.Zero)):
                        p0,p1 = p1,p0
                        p0dict,p1dict=p1dict,p0dict
                        r0,r1 = r1,r0
                    if self.equal(p0dict.get((1,0),S.Zero),p1dict.get((0,1),S.Zero)) and self.equal(p0dict.get((0,1),S.Zero),-p1dict.get((1,0),S.Zero)):
                        # p0+tvar*p1, p1-tvar*p0
                        # subs: tvar*svar + cvar = 1, svar-tvar*cvar=tvar
                        neweqs.append([Poly(p0dict.get((1,0),S.Zero) + p0dict.get((0,1),S.Zero)*tvar + p0.TC() + tvar*p1.TC(),*symbols), Poly(r0+tvar*r1,*othersymbols)])
                        neweqs.append([Poly(p0dict.get((1,0),S.Zero)*tvar - p0dict.get((0,1),S.Zero) - p0.TC()*tvar + p1.TC(),*symbols), Poly(r1-tvar*r0,*othersymbols)])
                        unusedindices.remove(i)
                        unusedindices.remove(j)
                        break
            if len(neweqs) >= 8:
                break
            log.warn('allowedindex %d found %d equations where coefficients of equations match', allowedindex, len(neweqs))
            
        if len(neweqs) < 8:
            raise self.CannotSolveError('found %d equations where coefficients of equations match! need at least 8'%len(neweqs))

        mysubs = []
        badjointvars = []
        for solvejointvar in solvejointvars:
            varsubs = self.Variable(solvejointvar).subs
            # only choose if varsubs has entry in originalsymbols or othersymbols
            if len([s for s in varsubs if s[1] in originalsymbols+othersymbols]) > 0:
                mysubs += varsubs
            else:
                badjointvars.append(solvejointvar)

        AllEquationsExtra = [eq for eq in AllEquationsExtra if not eq.has(*badjointvars)]
        AllPolyEquationsExtra = []
        for eq in AllEquationsExtra:
            mysubs = []
            for solvejointvar in solvejointvars:
                mysubs += self.Variable(solvejointvar).subs
            peq = Poly(eq.subs(mysubs), rawpolyeqs[0][0].gens)
            mixed = False
            for monom, coeff in peq.terms():
                if sum(monom) > 0:
                    # make sure coeff doesn't have any symbols from
                    if coeff.has(*rawpolyeqs[0][1].gens):
                        mixed = True
                        break
            if not mixed:
                AllPolyEquationsExtra.append((peq - peq.TC(), Poly(-peq.TC(), rawpolyeqs[0][1].gens)))
            
        for polyeq in [polyeqs[ipeq] for ipeq in unusedindices] + AllPolyEquationsExtra:
            p0 = Poly(polyeq[0],cvar,svar)
            p1 = polyeq[1]
            # need to substitute cvar and svar with tvar
            maxdenom = 0
            for monoms in p0.monoms():
                maxdenom=max(maxdenom,monoms[0]+monoms[1])
            eqnew = S.Zero
            for monoms,c in p0.terms():
                term = c*((1-tvar**2)**monoms[0])*(2*tvar)**monoms[1]*(1+tvar**2)**(maxdenom-monoms[0]-monoms[1])
                eqnew += term
            neweqs.append([Poly(eqnew,*symbols),Poly(p1.as_expr()*(1+tvar**2)**maxdenom,*othersymbols)])
            neweqs.append([Poly(eqnew*tvar,*symbols),Poly(p1.as_expr()*tvar*(1+tvar**2)**maxdenom,*othersymbols)])
        for ipeq,peq in enumerate(rawpolyeqs):
            if not peq[0].has(cvar,svar):
                neweqs.append([Poly(peq[0],*symbols),Poly(peq[1],*othersymbols)])
                neweqs.append([Poly(peq[0].as_expr()*tvar,*symbols),Poly(peq[1].as_expr()*tvar,*othersymbols)])
                
        # according to theory, neweqs should have 20 equations, however this isn't always the case
        # one side should have only numbers, this makes the following inverse operations trivial
        for peq in neweqs:
            peq0dict = peq[0].as_dict()
            peq[1] = peq[1] - tvar*peq0dict.get((0,0,0,0,1),S.Zero)-peq[0].TC()
            peq[0] = peq[0] - tvar*peq0dict.get((0,0,0,0,1),S.Zero)-peq[0].TC()
            
        hasreducedeqs = True
        while hasreducedeqs:
            self._CheckPreemptFn(progress = 0.08)
            hasreducedeqs = False
            for ipeq,peq in enumerate(neweqs):
                peq0dict = peq[0].as_dict()
                if len(peq0dict) == 1:
                    monomkey = peq0dict.keys()[0]
                    monomcoeff = peq0dict[monomkey]
                    monomvalue = peq[1].as_expr()
                    if sympy_smaller_073:
                        monomexpr = Monomial(*monomkey).as_expr(*peq[0].gens)
                    else:
                        monomexpr = Monomial(monomkey).as_expr(*peq[0].gens)
                    # for every equation that has this monom, substitute it
                    for ipeq2, peq2 in enumerate(neweqs):
                        if ipeq == ipeq2:
                            continue
                        for monoms,c in peq2[0].terms():
                            if monoms == monomkey:
                                # have to remove any common expressions between c and monomcoeff, or else equation can get huge
                                num2, denom2 = fraction(cancel(c/monomcoeff))
                                denom3, num3 = fraction(cancel(monomcoeff/c))
                                if denom2.is_number and denom3.is_number and abs(denom2.evalf()) > abs(denom3.evalf()): # have to select one with least abs value, or else equation will skyrocket
                                    denom2 = denom3
                                    num2 = num3
                                # have to be careful when multiplying or equation magnitude can get really skewed
                                if denom2.is_number and abs(denom2.evalf()) > 100:
                                    peq2[0] = (peq2[0] - c*monomexpr)*monomcoeff
                                    peq2[1] = peq2[1]*monomcoeff - c*monomvalue
                                else:
                                    peq2[0] = (peq2[0] - c*monomexpr)*denom2
                                    peq2[1] = peq2[1]*denom2 - num2*monomvalue
                                hasreducedeqs = True
                                break
            # see if there's two equations with two similar monomials on the left-hand side
            # observed problem: coefficients become extremely huge (100+ digits), need a way to simplify them
#             for ipeq,peq in enumerate(neweqs):
#                 peq0monoms = peq[0].monoms()
#                 if peq[0] != S.Zero and len(peq0monoms) == 2:
#                     for ipeq2, peq2 in enumerate(neweqs):
#                         if ipeq2 == ipeq:
#                             continue
#                         if peq0monoms == peq2[0].monoms():
#                             peqdict = peq[0].as_dict()
#                             peq2dict = peq2[0].as_dict()
#                             monom0num, monom0denom = fraction(cancel(peqdict[peq0monoms[0]]/peq2dict[peq0monoms[0]]))
#                             peqdiff = peq2[0]*monom0num - peq[0]*monom0denom
#                             #peqdiff = peq2[0]*peqdict[peq0monoms[0]] - peq[0]*peq2dict[peq0monoms[0]]
#                             if peqdiff != S.Zero:
#                                 # there's one monomial left
#                                 peqright = (peq2[1]*monom0num - peq[1]*monom0denom)
#                                 if peqdiff.LC() != S.Zero:
#                                     # check if peqdiff.LC() divides everything cleanly
#                                     q0, r0 = div(peqright, peqdiff.LC())
#                                     if r0 == S.Zero:
#                                         q1, r1 = div(peqdiff, peqdiff.LC())
#                                         if r1 == S.Zero:
#                                             peqright = Poly(q0, *peqright.gens)
#                                             peqdiff = Poly(q1, *peqdiff.gens)
#                                 # now solve for the other variable
#                                 monom1num, monom1denom = fraction(cancel(peqdict[peq0monoms[1]]/peq2dict[peq0monoms[1]]))
#                                 peq2diff = peq2[0]*monom1num - peq[0]*monom1denom
#                                 peq2right = (peq2[1]*monom1num - peq[1]*monom1denom)
#                                 if peq2diff.LC() != S.Zero:
#                                     # check if peqdiff.LC() divides everything cleanly
#                                     q0, r0 = div(peq2right, peq2diff.LC())
#                                     if r0 == S.Zero:
#                                         q1, r1 = div(peq2diff, peq2diff.LC())
#                                         if r1 == S.Zero:
#                                             peq2right = Poly(q0, *peq2right.gens)
#                                             peq2diff = Poly(q1, *peq2diff.gens)
#                                 eqdiff, eqdiffcommon = self.removecommonexprs(peqdiff.as_expr(),returncommon=True, onlygcd=False)
#                                 eqright, eqrightcommon = self.removecommonexprs(peqright.as_expr(),returncommon=True, onlygcd=False)
#                                 eqdiffmult = cancel(eqdiffcommon/eqrightcommon)
#                                 peq[0] = Poly(eqdiff*eqdiffmult, *peqdiff.gens)
#                                 peq[1] = Poly(eqright, *peqright.gens)
#                                 
#                                 eq2diff, eq2diffcommon = self.removecommonexprs(peq2diff.as_expr(),returncommon=True, onlygcd=False)
#                                 eq2right, eq2rightcommon = self.removecommonexprs(peq2right.as_expr(),returncommon=True, onlygcd=False)
#                                 eq2diffmult = cancel(eq2diffcommon/eq2rightcommon)
#                                 peq2[0] = Poly(eq2diff*eq2diffmult, *peq2diff.gens)
#                                 peq2[1] = Poly(eq2right, *peq2right.gens)
#                                 hasreducedeqs = True
#                                 break
#                             else:
#                                 # overwrite peq2 in case there are others
#                                 peq2[0] = peqdiff
#                                 peq2[1] = peq2[1]*monom0num - peq[1]*monom0denom
#                                 hasreducedeqs = True

        neweqs_full = []
        reducedeqs = []
        # filled with equations where one variable is singled out
        reducedsinglevars = [None,None,None,None]
        for ipeq, peq in enumerate(neweqs):
            peqcomb = Poly(peq[1].as_expr()-peq[0].as_expr(), peq[0].gens[:-1] + peq[1].gens)
            minimummonom = None
            for monom in (peqcomb).monoms():
                if minimummonom is None:
                    minimummonom = monom
                else:
                    minimummonom = [min(minimummonom[i], monom[i]) for i in range(len(monom))]
            
            if minimummonom is None:
                continue
            
            diveq = None
            for i in range(len(minimummonom)):
                if minimummonom[i] > 0:
                    if diveq is None:
                        diveq = peqcomb.gens[i]**minimummonom[i]
                    else:
                        diveq *= peqcomb.gens[i]**minimummonom[i]
            
            if diveq is not None:
                log.info(u'assuming equation %r is non-zero, dividing by %r', diveq, peqcomb)
                peqcombnum, r = div(peqcomb, diveq)
                assert(r==S.Zero)
                peqcombold = peqcomb # save for debugging
                peqcomb = Poly(peqcombnum, peqcomb.gens)#Poly(peqcomb / diveq, peqcomb.gens)
                peq0norm, r = div(peq[0], diveq)
                assert(r==S.Zero)
                peq1norm, r = div(peq[1], diveq)
                assert(r==S.Zero)
                peq = (Poly(peq0norm, *peq[0].gens), Poly(peq1norm, *peq[1].gens))

            coeff, factors = peqcomb.factor_list()
            
            # check if peq[1] can factor out certain monoms                
            if len(factors) > 1:
                # if either of the factors evaluate to 0, then we are ok
                # look for trivial factors that evaluate to 0 or some constant expression and put those into the checkforzeros
                eq = S.One
                divisoreq = S.One
                newzeros = []
                for factor, fdegree in factors:
                    if sum(factor.degree_list()) == 1:
                        # actually causes fractions to blow up, so don't use
                        #if factor.as_expr().has(*(originalsymbols+othersymbols)):
                        #    eq *= factor.as_expr()
                        #    continue
                        log.info(u'assuming equation %r is non-zero', factor)
                        newzeros.append(factor.as_expr())
                        divisoreq *= factor.as_expr()
                    else:
                        eq *= factor.as_expr()
                eq = coeff*eq.expand() # have to multiply by the coeff, or otherwise the equation will be weighted different and will be difficult to determine epsilons
                if peq[0] != S.Zero:
                    peq0norm, r = div(peq[0], divisoreq)
                    assert(r==S.Zero)
                    peq1norm, r = div(peq[1], divisoreq)
                    assert(r==S.Zero)
                    peq0norm = Poly(peq0norm, *peq[0].gens)
                    peq1norm = Poly(peq1norm, *peq[1].gens)
                    peq0dict = peq0norm.as_dict()
                    monom, value = peq0dict.items()[0]
                    if len(peq0dict) == 1 and __builtin__.sum(monom) == 1:
                        indices = [index for index in range(4) if monom[index] == 1]
                        if len(indices) > 0 and indices[0] < 4:
                            reducedsinglevars[indices[0]] = (value, peq1norm.as_expr())
                    isunique = True
                    for test0, test1 in neweqs_full:
                        if (self.equal(test0,peq0norm) and self.equal(test1,peq1norm)) or (self.equal(test0,-peq0norm) and self.equal(test1,-peq1norm)):
                            isunique = False
                            break
                    if isunique:
                        neweqs_full.append((peq0norm, peq1norm))
                    else:
                        log.info('not unique: %r', eq)
                else:
                    eq = eq.subs(self.freevarsubs)
                    if self.CheckExpressionUnique(reducedeqs, eq):
                        reducedeqs.append(eq)
                    else:
                        log.info('factors %d not unique: %r', len(factors), eq)
            else:
                if peq[0] != S.Zero:
                    peq0dict = peq[0].as_dict()
                    monom, value = peq0dict.items()[0]
                    if len(peq0dict) == 1 and __builtin__.sum(monom) == 1:
                        indices = [index for index in range(4) if monom[index] == 1]
                        if len(indices) > 0 and indices[0] < 4:
                            reducedsinglevars[indices[0]] = (value,peq[1].as_expr())
                    
                    isunique = True
                    for test0, test1 in neweqs_full:
                        if (self.equal(test0,peq[0]) and self.equal(test1,peq[1])) or (self.equal(test0,-peq[0]) and self.equal(test1,-peq[1])):
                            isunique = False
                            break
                    if isunique:
                        neweqs_full.append(peq)
                    else:
                        log.info('not unique: %r', peq)
                else:
                    eq = peq[1].as_expr().subs(self.freevarsubs)
                    if self.CheckExpressionUnique(reducedeqs, eq):
                        reducedeqs.append(eq)
                    else:
                        log.info('factors %d reduced not unique: %r', len(factors), eq)
        for ivar in range(2):
            if reducedsinglevars[2*ivar+0] is not None and reducedsinglevars[2*ivar+1] is not None:
                # a0*cos = b0, a1*sin = b1
                a0,b0 = reducedsinglevars[2*ivar+0]
                a1,b1 = reducedsinglevars[2*ivar+1]
                reducedeqs.append((b0*a1)**2 + (a0*b1)**2 - (a0*a1)**2)
                        
        haszeroequations = len(reducedeqs)>0
        
        neweqs_simple = []
        neweqs_complex = []
        for peq in neweqs_full:
            hassquare = False
            for monom in peq[0].monoms():
                if any([m > 1 for m in monom]):
                    hassquare = True
            if not hassquare:
                neweqs_simple.append(peq)
            else:
                neweqs_complex.append(peq)

        # add more equations by multiplying tvar. this makes it possible to have a fuller matrix
        neweqs2 = neweqs_simple + [(Poly(peq[0]*tvar, peq[0].gens), Poly(peq[1]*tvar, peq[1].gens)) for peq in neweqs_simple if not peq[0].has(tvar)]
        
        # check hacks for 5dof komatsu ik
        if 1:
            for itest in range(0,len(neweqs_simple)-1,2):
                if neweqs_simple[itest][0]*tvar-neweqs_simple[itest+1][0] == S.Zero:
                    eq = (neweqs_simple[itest][1]*tvar-neweqs_simple[itest+1][1]).as_expr()
                    if eq != S.Zero and self.CheckExpressionUnique(reducedeqs, eq):
                        reducedeqs.append(eq)
                if neweqs_simple[itest+1][0]*tvar-neweqs_simple[itest][0] == S.Zero:
                    eq = (neweqs_simple[itest+1][1]*tvar-neweqs_simple[itest][1]).as_expr()
                    if eq != S.Zero and self.CheckExpressionUnique(reducedeqs, eq):
                        reducedeqs.append(eq)
            for testrational in [Rational(-103651, 500000), Rational(-413850340369, 2000000000000), Rational(151,500), Rational(301463, 1000000)]:
                if ((neweqs_simple[0][0]*tvar - neweqs_simple[1][0])*testrational + neweqs_simple[6][0]*tvar - neweqs_simple[7][0]).expand() == S.Zero:
                    if (neweqs_simple[0][0]*tvar - neweqs_simple[1][0]) == S.Zero:
                        eq = (neweqs_simple[6][1]*tvar - neweqs_simple[7][1]).expand().as_expr()
                    else:
                        eq = ((neweqs_simple[0][1]*tvar - neweqs_simple[1][1])*testrational + neweqs_simple[6][1]*tvar - neweqs_simple[7][1]).expand().as_expr()
                    if self.CheckExpressionUnique(reducedeqs, eq):
                        reducedeqs.append(eq)
                if ((neweqs_simple[0][0]*tvar - neweqs_simple[1][0])*testrational + (neweqs_simple[2][0]*tvar-neweqs_simple[3][0])*sqrt(2)) == S.Zero:
                    if (neweqs_simple[0][0]*tvar - neweqs_simple[1][0]) == S.Zero:
                        eq = ((neweqs_simple[2][1]*tvar-neweqs_simple[3][1])*sqrt(2)).as_expr()
                    else:
                        eq = ((neweqs_simple[0][1]*tvar - neweqs_simple[1][1])*testrational + (neweqs_simple[2][1]*tvar-neweqs_simple[3][1])*sqrt(2)).as_expr()
                    if self.CheckExpressionUnique(reducedeqs, eq):
                        reducedeqs.append(eq)
                    
        neweqs_test = neweqs2#neweqs_simple
        
        allmonoms = set()
        for ipeq, peq in enumerate(neweqs_test):
            allmonoms = allmonoms.union(set(peq[0].monoms()))
        allmonoms = list(allmonoms)
        allmonoms.sort()
        
        if len(allmonoms) > len(neweqs_full) and len(reducedeqs) < 3:
            raise self.CannotSolveError('new monoms is %d>%d, reducedeqs=%d'%(len(allmonoms), len(neweqs_full), len(reducedeqs)))
        
        # the equations are ginac objects
        getsubs = None
        dictequations = []
        preprocesssolutiontree = []
        localsymbolmap = {}
        AUinv = None
        if len(allmonoms) < len(neweqs_test):
            # order with respect to complexity of [0], this is to make the inverse of A faster
            complexity = [(self.codeComplexity(peq[0].as_expr()),peq) for peq in neweqs_test]
            complexity.sort(key=itemgetter(0))
            neweqs_test = [peq for c,peq in complexity]
            A = zeros((len(neweqs_test),len(allmonoms)))
            B = zeros((len(neweqs_test),1))
            for ipeq,peq in enumerate(neweqs_test):
                for m,c in peq[0].terms():
                    A[ipeq,allmonoms.index(m)] = c.subs(self.freevarsubs)
                B[ipeq] = peq[1].as_expr().subs(self.freevarsubs)
            AU = zeros((len(allmonoms),len(allmonoms)))
            AL = zeros((A.shape[0]-len(allmonoms),len(allmonoms)))
            BU = zeros((len(allmonoms),1))
            BL = zeros((A.shape[0]-len(allmonoms),1))
            AUadjugate = None
            AU = A[:A.shape[1],:]
            nummatrixsymbols = 0
            numcomplexpows = 0 # for non-symbols, how many non-integer pows there are
            for a in AU:
                if not a.is_number:
                    nummatrixsymbols += 1
                    continue
                
                hascomplexpow = False
                for poweq in a.find(Pow):
                    if poweq.exp != S.One and poweq.exp != -S.One:
                        hascomplexpow = True
                        break
                if hascomplexpow:
                    numcomplexpows += 1
            
            # the 150 threshold is a guess
            if nummatrixsymbols > 150:
                log.info('found a non-singular matrix with %d symbols, but most likely there is a better one', nummatrixsymbols)
                raise self.CannotSolveError('matrix has too many symbols (%d), giving up since most likely will freeze'%nummatrixsymbols)
                    
            log.info('matrix has %d symbols', nummatrixsymbols)
            if nummatrixsymbols > 10:
                # if matrix symbols are great, yield so that other combinations can be tested?
                pass
            
            AUdetmat = None
            if self.IsDeterminantNonZeroByEval(AU):
                rows = range(A.shape[1])
                AUdetmat = AU
            elif not self.IsDeterminantNonZeroByEval(A.transpose()*A):
                raise self.CannotSolveError('coefficient matrix is singular')
            
            else:
                # prune the dependent vectors
                AU = A[0:1,:]
                rows = [0]
                for i in range(1,A.shape[0]):
                    self._CheckPreemptFn(progress = 0.09)
                    AU2 = AU.col_join(A[i:(i+1),:])
                    if AU2.shape[0] == AU2.shape[1]:
                        AUdetmat = AU2
                    else:
                        AUdetmat = AU2*AU2.transpose()
                    # count number of fractions/symbols
                    numausymbols = 0
                    numaufractions = 0
                    for f in AUdetmat:
                        if not f.is_number:
                            numausymbols += 1
                        if f.is_rational and not f.is_integer:
                            numaufractions += 1
                            # if fraction is really huge, give it more counts (the bigger it is, the slower it takes to compute)
                            flength = len(str(f))
                            numaufractions += int(flength/20)
                    #d = AUdetmat.det().evalf()
                    #if d == S.Zero:
                    if not self.IsDeterminantNonZeroByEval(AUdetmat, len(rows)>9 and (numaufractions > 120 or numaufractions+numausymbols > 120)):
                        log.info('skip dependent index %d, numausymbols=%d, numausymbols=%d', i,numaufractions,numausymbols)
                        continue
                    AU = AU2
                    rows.append(i)
                    if AU.shape[0] == AU.shape[1]:
                        break
                if AU.shape[0] != AU.shape[1]:
                    raise self.CannotSolveError('could not find non-singular matrix %r'%(AU.shape,))
                
            otherrows = range(A.shape[0])
            for i,row in enumerate(rows):
                BU[i] = B[row]
                otherrows.remove(row)
            for i,row in enumerate(otherrows):
                BL[i] = B[row]
                AL[i,:] = A[row,:]
                
            if 0:#self.has(A,*self.freevars):
                AUinv = AU.inv()
                AUdet = AUdetmat.det()
                log.info('AU has symbols, so working with inverse might take some time')
                AUdet = self.trigsimp(AUdet.subs(self.freevarsubsinv),self.freejointvars).subs(self.freevarsubs)
                # find the adjugate by simplifying from the inverse
                AUadjugate = zeros(AUinv.shape)
                sinsubs = []
                for freevar in self.freejointvars:
                    var=self.Variable(freevar)
                    for ideg in range(2,40):
                        if ideg % 2:
                            sinsubs.append((var.cvar**ideg,var.cvar*(1-var.svar**2)**int((ideg-1)/2)))
                        else:
                            sinsubs.append((var.cvar**ideg,(1-var.svar**2)**(ideg/2)))
                for i in range(AUinv.shape[0]):
                    log.info('replacing row %d', i)
                    for j in range(AUinv.shape[1]):
                        numerator,denominator = self.recursiveFraction(AUinv[i,j])
                        numerator = self.trigsimp(numerator.subs(self.freevarsubsinv),self.freejointvars).subs(self.freevarsubs)
                        numerator, common = self.removecommonexprs(numerator,onlygcd=True,returncommon=True)
                        denominator = self.trigsimp((denominator/common).subs(self.freevarsubsinv),self.freejointvars).subs(self.freevarsubs)
                        try:
                            q,r=div(numerator*AUdet,denominator,self.freevars)
                        except PolynomialError, e:
                            # 1/(-9000000*cj16 - 9000000) contains an element of the generators set
                            raise self.CannotSolveError('cannot divide for matrix inversion: %s'%e)
                        
                        if r != S.Zero:
                            # sines and cosines can mix things up a lot, so converto to half-tan
                            numerator2, numerator2d, htvarsubsinv = self.ConvertSinCosEquationToHalfTan((AUdet*numerator).subs(sinsubs).expand().subs(sinsubs).expand().subs(sinsubs).expand(), self.freejointvars)
                            denominator2, denominator2d, htvarsubsinv = self.ConvertSinCosEquationToHalfTan(denominator.subs(sinsubs).expand().subs(sinsubs).expand().subs(sinsubs).expand(), self.freejointvars)
                            extranumerator, extradenominator = fraction(numerator2d/denominator2d)
                            htvars = [v for v,eq in htvarsubsinv]
                            q,r=div((numerator2*extradenominator).expand(),(denominator2).expand(),*htvars)
                            if r != S.Zero:
                                log.warn('cannot get rid of denominator for element (%d, %d) in (%s/%s)',i, j, numerator2,denominator2)
                                #raise self.CannotSolveError('cannot get rid of denominator')
                                
                            # convert back to cos/sin in order to get rid of the denominator term?
                            sym = self.gsymbolgen.next()
                            dictequations.append((sym, q / extranumerator))
                            q = sym
                            #div(q.subs(htvarsubsinv).expand(), extranumerator.subs(htvarsubsinv).expand(), *self.freevars)
                            #newsubs=[(Symbol('htj4'), sin(self.freejointvars[0])/(1+cos(self.freejointvars[0])))]
                            #div(q.extranumerator

                        AUadjugate[i,j] = self.trigsimp(q.subs(self.freevarsubsinv),self.freejointvars).subs(self.freevarsubs)
                checkforzeros.append(self.removecommonexprs(AUdet,onlygcd=False,onlynumbers=True))
                # reason we're multiplying by adjugate instead of inverse is to get rid of the potential divides by (free) parameters
                BUresult = AUadjugate*BU
                C = AL*BUresult-BL*AUdet
                for c in C:
                    reducedeqs.append(c)
            else:
                # usually if nummatrixsymbols == 0, we would just solve the inverse of the matrix. however if non-integer powers get in the way, we have to resort to solving the matrix dynamically...
                if nummatrixsymbols + numcomplexpows/4 > 40:
                    Asymbols = []
                    for i in range(AU.shape[0]):
                        Asymbols.append([Symbol('gclwh%d_%d'%(i,j)) for j in range(AU.shape[1])])
                    matrixsolution = AST.SolverMatrixInverse(A=AU,Asymbols=Asymbols)
                    getsubs = matrixsolution.getsubs
                    preprocesssolutiontree.append(matrixsolution)
                    self.usinglapack = True
                    # evaluate the inverse at various solutions and see which entries are always zero
                    isnotzero = zeros((AU.shape[0],AU.shape[1]))
                    epsilon = 1e-15
                    epsilondet = 1e-30
                    hasOneNonSingular = False
                    for itest,subs in enumerate(self.testconsistentvalues):
                        AUvalue = AU.subs(subs)
                        isallnumbers = True
                        for f in AUvalue:
                            if not f.is_number:
                                isallnumbers = False
                                break
                        if isallnumbers:
                            # compute more precise determinant
                            AUdetvalue = AUvalue.det()
                        else:
                            AUdetvalue = AUvalue.evalf().det().evalf()
                        if abs(AUdetvalue) > epsilondet:# != S.Zero:
                            hasOneNonSingular = True
                            AUinvvalue = AUvalue.evalf().inv()
                            for i in range(AUinvvalue.shape[0]):
                                for j in range(AUinvvalue.shape[1]):
                                    # since making numerical approximations, need a good value for zero
                                    if abs(AUinvvalue[i,j]) > epsilon:#!= S.Zero:
                                        isnotzero[i,j] = 1
                    if not hasOneNonSingular:
                        raise self.CannotSolveError('inverse matrix is always singular')
                    
                    AUinv = zeros((AU.shape[0],AU.shape[1]))
                    for i in range(AUinv.shape[0]):
                        for j in range(AUinv.shape[1]):
                            if isnotzero[i,j] == 0:
                                Asymbols[i][j] = None
                            else:
                                AUinv[i,j] = Asymbols[i][j]
                    BUresult = AUinv*BU
                    C = AL*BUresult-BL
                    for c in C:
                        reducedeqs.append(c)                        
                elif 0:#nummatrixsymbols > 60:
                    # requires swiginac
                    getsubs = lambda valuesubs: self.SubstituteGinacEquations(dictequations, valuesubs, localsymbolmap)
                    # cannot compute inverse since too many symbols
                    log.info('lu decomposition')
                    # PA = L DD**-1 U
                    P, L, DD, U = self.LUdecompositionFF(AU,*self.pvars)
                    log.info('lower triangular solve')
                    res0 = L.lower_triangular_solve(P*BU)
                    # have to use ginac, since sympy is too slow
                    # there are divides in res0, so have to simplify
                    gres1 = swiginac.symbolic_matrix(len(res0),1,'gres1')
                    for i in range(len(res0)):
                        gres0i = GinacUtils.ConvertToGinac(res0[i],localsymbolmap)
                        gDDi = GinacUtils.ConvertToGinac(DD[i,i],localsymbolmap)
                        gres1[i,0] = gres0i*gDDi
                    gothersymbols = [localsymbolmap[s.name] for s in othersymbols if s.name in localsymbolmap]
                    res2 = []
                    gres2 = swiginac.symbolic_matrix(len(res0),1,'gres2')
                    for icol in range(len(gres1)):
                        log.info('extracting poly monoms from L solving: %d', icol)
                        polyterms = GinacUtils.GetPolyTermsFromGinac(gres1[icol],gothersymbols,othersymbols)
                        # create a new symbol for every term
                        eq = S.Zero
                        for monom, coeff in polyterms.iteritems():
                            sym = self.gsymbolgen.next()
                            dictequations.append((sym,coeff))
                            localsymbolmap[sym.name] = swiginac.symbol(sym.name)
                            if sympy_smaller_073:
                                eq += sym*Monomial(*monom).as_expr(*othersymbols)
                            else:
                                eq += sym*Monomial(monom).as_expr(*othersymbols)
                        res2.append(eq)
                        gres2[icol] = GinacUtils.ConvertToGinac(eq,localsymbolmap)
                        
                    gU = GinacUtils.ConvertMatrixToGinac(U,'U',localsymbolmap)
                    log.info('upper triangular solve')
                    gres3 = GinacUtils.SolveUpperTriangular(gU, gres2, 'gres3')
                    res3 = []
                    for icol in range(len(gres3)):
                        log.info('extracting poly monoms from U solving: %d', icol)
                        polyterms = GinacUtils.GetPolyTermsFromGinac(gres3[icol],gothersymbols,othersymbols)
                        # create a new symbol for every term
                        eq = S.Zero
                        for monom, coeff in polyterms.iteritems():
                            sym = self.gsymbolgen.next()
                            dictequations.append((sym,coeff))
                            localsymbolmap[sym.name] = swiginac.symbol(sym.name)
                            if sympy_smaller_073:
                                eq += sym*Monomial(*monom).as_expr(*othersymbols)
                            else:
                                eq += sym*Monomial(monom).as_expr(*othersymbols)
                        res3.append(eq)
                    BUresult = Matrix(gres3.rows(),gres3.cols(),res3)
                    C = AL*BUresult-BL
                    for c in C:
                        reducedeqs.append(c)
                else:
                    # if AU has too many fractions, it can prevent computation
                    allzeros = True
                    for b in BU:
                        if b != S.Zero:
                            allzeros = False
                    if not allzeros:
                        try:
                            AUinv = AU.inv()
                        except ValueError, e:
                            raise self.CannotSolveError(u'failed to invert matrix: %e'%e)
                        
                        BUresult = AUinv*BU
                        C = AL*BUresult-BL
                    else:
                        C = -BL
                    for c in C:
                        if c != S.Zero:
                            reducedeqs.append(c)
            log.info('computed non-singular AU matrix')
        
        if len(reducedeqs) == 0:
            raise self.CannotSolveError('reduced equations are zero')
        
        # is now a (len(neweqs)-len(allmonoms))x1 matrix, usually this is 4x1
        htvars = []
        htvarsubs = []
        htvarsubs2 = []
        usedvars = []
        htvarcossinoffsets = []
        nonhtvars = []
        for iothersymbol, othersymbol in enumerate(othersymbols):
            if othersymbol.name[0] == 'c':
                assert(othersymbols[iothersymbol+1].name[0] == 's')
                htvarcossinoffsets.append(iothersymbol)
                name = othersymbol.name[1:]
                htvar = Symbol('ht%s'%name)
                htvarsubs += [(othersymbol,(1-htvar**2)/(1+htvar**2)),(othersymbols[iothersymbol+1],2*htvar/(1+htvar**2))]
                htvars.append(htvar)
                htvarsubs2.append((Symbol(name),2*atan(htvar)))
                usedvars.append(Symbol(name))
            elif othersymbol.name[0] != 'h' and othersymbol.name[0] != 's':
                # not half-tan, sin, or cos
                nonhtvars.append(othersymbol)
                usedvars.append(othersymbol)
        htvarsubs += [(cvar,(1-tvar**2)/(1+tvar**2)),(svar,2*tvar/(1+tvar**2))]
        htvars.append(tvar)
        htvarsubs2.append((Symbol(varname),2*atan(tvar)))
        usedvars.append(Symbol(varname))
        
        if haszeroequations:
            log.info('special structure in equations detected, try to solve through elimination')
            AllEquations = [eq.subs(self.invsubs) for eq in reducedeqs if self.codeComplexity(eq) < 2000]
            for curvar in usedvars[:-1]:
                try:
                    unknownvars = usedvars[:]
                    unknownvars.remove(curvar)
                    jointtrees2=[]
                    curvarsubs=self.Variable(curvar).subs
                    treefirst = self.SolveAllEquations(AllEquations,curvars=[curvar],othersolvedvars=self.freejointvars,solsubs=self.freevarsubs[:],endbranchtree=[AST.SolverSequence([jointtrees2])],unknownvars=unknownvars+[tvar], canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                    # solvable, which means we now have len(AllEquations)-1 with two variables, solve with half angles
                    halfanglesolution=self.SolvePairVariablesHalfAngle(raweqns=[eq.subs(curvarsubs) for eq in AllEquations],var0=unknownvars[0],var1=unknownvars[1],othersolvedvars=self.freejointvars+[curvar])[0]
                    # sometimes halfanglesolution can evaluate to all zeros (katana arm), need to catch this and go to a different branch
                    halfanglesolution.AddHalfTanValue = True
                    jointtrees2.append(halfanglesolution)
                    halfanglevar = unknownvars[0] if halfanglesolution.jointname==unknownvars[0].name else unknownvars[1]
                    unknownvars.remove(halfanglevar)
                    
                    try:
                        # give that two variables are solved, can most likely solve the rest. Solving with the original
                        # equations yields simpler solutions since reducedeqs hold half-tangents
                        curvars = solvejointvars[:]
                        curvars.remove(curvar)
                        curvars.remove(halfanglevar)
                        subsinv = []
                        for v in solvejointvars:
                            subsinv += self.Variable(v).subsinv
                        AllEquationsOrig = [(peq[0].as_expr()-peq[1].as_expr()).subs(subsinv) for peq in rawpolyeqs]
                        self.sortComplexity(AllEquationsOrig)
                        jointtrees2 += self.SolveAllEquations(AllEquationsOrig,curvars=curvars,othersolvedvars=self.freejointvars+[curvar,halfanglevar],solsubs=self.freevarsubs+curvarsubs+self.Variable(halfanglevar).subs,endbranchtree=endbranchtree, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                        return preprocesssolutiontree+solutiontree+treefirst,solvejointvars
                    
                    except self.CannotSolveError,e:
                        # try another strategy
                        log.debug(e)
                        
                    # solve all the unknowns now
                    jointtrees3=[]
                    treesecond = self.SolveAllEquations(AllEquations,curvars=unknownvars,othersolvedvars=self.freejointvars+[curvar,halfanglevar],solsubs=self.freevarsubs+curvarsubs+self.Variable(halfanglevar).subs,endbranchtree=[AST.SolverSequence([jointtrees3])], canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                    for t in treesecond:
                        # most likely t is a solution...
                        t.AddHalfTanValue = True
                        if isinstance(t,AST.SolverCheckZeros):
                            for t2 in t.zerobranch:
                                t2.AddHalfTanValue = True
                            for t2 in t.nonzerobranch:
                                t2.AddHalfTanValue = True
                            if len(t.zerobranch) == 0 or isinstance(t.zerobranch[0],AST.SolverBreak):
                                log.info('detected zerobranch with SolverBreak, trying to fix')
                                
                    jointtrees2 += treesecond
                    # using these solutions, can evaluate all monoms and check for consistency, this step is crucial since
                    # AllEquations might not constrain all degrees of freedom (check out katana)
                    indices = []
                    for i in range(4):
                        monom = [0]*len(symbols)
                        monom[i] = 1
                        indices.append(allmonoms.index(tuple(monom)))
                    if AUinv is not None:
                        X = AUinv*BU
                        for i in [0,2]:
                            jointname=symbols[i].name[1:]
                            try:
                                # atan2(0,0) produces an invalid solution
                                jointtrees3.append(AST.SolverSolution(jointname,jointeval=[atan2(X[indices[i+1]],X[indices[i]])],isHinge=self.IsHinge(jointname)))
                                usedvars.append(Symbol(jointname))
                            except Exception, e:
                                log.warn(e)
                                
                        jointcheckeqs = []
                        for i,monom in enumerate(allmonoms):
                            if not i in indices:
                                eq = S.One
                                for isymbol,ipower in enumerate(monom):
                                    eq *= symbols[isymbol]**ipower
                                jointcheckeqs.append(eq-X[i])
                        # threshold can be a little more loose since just a sanity check
                        jointtrees3.append(AST.SolverCheckZeros('sanitycheck',jointcheckeqs,zerobranch=endbranchtree,nonzerobranch=[AST.SolverBreak('sanitycheck for solveLiWoernleHiller')],anycondition=False,thresh=0.001))
                        return preprocesssolutiontree+solutiontree+treefirst,usedvars
                    else:
                        log.warn('AUinv not initialized, perhaps missing important equations')
                        
                except self.CannotSolveError,e:
                    log.info(e)
                
            try:
                log.info('try to solve first two variables pairwise')
                
                #solution = self.SolvePairVariables(AllEquations,usedvars[0],usedvars[1],self.freejointvars,maxcomplexity=50)
                jointtrees=[]
                unusedvars = [s for s in solvejointvars if not s in usedvars]
                raweqns=[eq for eq in AllEquations if not eq.has(tvar, *unusedvars)]
                if len(raweqns) > 1:
                    halfanglesolution = self.SolvePairVariablesHalfAngle(raweqns=raweqns,var0=usedvars[0],var1=usedvars[1],othersolvedvars=self.freejointvars)[0]
                    halfanglevar = usedvars[0] if halfanglesolution.jointname==usedvars[0].name else usedvars[1]
                    unknownvar = usedvars[1] if halfanglesolution.jointname==usedvars[0].name else usedvars[0]
                    nexttree = self.SolveAllEquations(raweqns,curvars=[unknownvar],othersolvedvars=self.freejointvars+[halfanglevar],solsubs=self.freevarsubs+self.Variable(halfanglevar).subs,endbranchtree=[AST.SolverSequence([jointtrees])], canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                    #finalsolution = self.solveSingleVariable(AllEquations,usedvars[2],othersolvedvars=self.freejointvars+usedvars[0:2],maxsolutions=4,maxdegree=4)
                    try:
                        finaltree = self.SolveAllEquations(AllEquations,curvars=usedvars[2:],othersolvedvars=self.freejointvars+usedvars[0:2],solsubs=self.freevarsubs+self.Variable(usedvars[0]).subs+self.Variable(usedvars[1]).subs,endbranchtree=endbranchtree, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                        jointtrees += finaltree
                        return preprocesssolutiontree+[halfanglesolution]+nexttree,usedvars
                    
                    except self.CannotSolveError,e:
                        log.debug('failed to solve for final variable %s, so returning just two: %s'%(usedvars[2],str(usedvars[0:2])))
                        jointtrees += endbranchtree
                        # sometimes the last variable cannot be solved, so returned the already solved variables and let the higher function take care of it 
                        return preprocesssolutiontree+[halfanglesolution]+nexttree,usedvars[0:2]
                
            except self.CannotSolveError,e:
                log.debug(u'failed solving first two variables pairwise: %s', e)
                
        if len(reducedeqs) < 3:
            raise self.CannotSolveError('have need at least 3 reducedeqs (%d)'%len(reducedeqs))
        
        log.info('reducing %d equations', len(reducedeqs))
        newreducedeqs = []
        hassinglevariable = False
        for eq in reducedeqs:
            self._CheckPreemptFn(progress = 0.10)
            complexity = self.codeComplexity(eq)
            if complexity > 1500:
                log.warn('equation way too complex (%d), looking for another solution', complexity)
                continue
            
            if complexity > 1500:
                log.info('equation way too complex (%d), so try breaking it down', complexity)
                # don't support this yet...
                eq2 = eq.expand()
                assert(eq2.is_Add)
                log.info('equation has %d additions', len(eq2.args))
                indices = list(range(0, len(eq2.args), 100))
                indices[-1] = len(eq2.args)
                testpolyeqs = []
                startvalue = 0
                for nextvalue in indices[1:]:
                    log.info('computing up to %d', nextvalue)
                    testadd = S.Zero
                    for i in range(startvalue,nextvalue):
                        testadd += eq2.args[i]
                    testpolyeqs.append(Poly(testadd,*othersymbols))
                    startvalue = nextvalue
                # convert each poly's coefficients to symbols
                peq = Poly(S.Zero, *othersymbols)
                for itest, testpolyeq in enumerate(testpolyeqs):
                    log.info('adding equation %d', itest)
                    newpeq = Poly(S.Zero, *othersymbols)
                    for monom, coeff in newpeq.terms():
                        sym = self.gsymbolgen.next()
                        dictequations.append((sym,coeff))
                        if sympy_smaller_073:
                            newpeq += sym*Monomial(*monom).as_expr(*othersymbols)
                        else:
                            newpeq += sym*Monomial(monom).as_expr(*othersymbols)
                    peq += newpeq
            else:
                peq = Poly(eq,*othersymbols)
            maxdenom = [0]*len(htvarcossinoffsets)
            for monoms in peq.monoms():
                for i,ioffset in enumerate(htvarcossinoffsets):
                    maxdenom[i] = max(maxdenom[i],monoms[ioffset]+monoms[ioffset+1])
            eqnew = S.Zero
            for monoms,c in peq.terms():
                term = c
                for i,ioffset in enumerate(htvarcossinoffsets):
                    # for cos
                    num, denom = fraction(htvarsubs[2*i][1])
                    term *= num**monoms[ioffset]
                    # for sin
                    num, denom = fraction(htvarsubs[2*i+1][1])
                    term *= num**monoms[ioffset+1]
                # the denoms for sin/cos of the same joint variable are the same
                for i,ioffset in enumerate(htvarcossinoffsets):
                    denom = fraction(htvarsubs[2*i][1])[1]
                    term *= denom**(maxdenom[i]-monoms[ioffset]-monoms[ioffset+1])
                # multiply the rest of the monoms
                for imonom, monom in enumerate(monoms):
                    if not imonom in htvarcossinoffsets and not imonom-1 in htvarcossinoffsets:
                        # handle non-sin/cos variables yet
                        term *= othersymbols[imonom]**monom
                eqnew += term
            newpeq = Poly(eqnew,htvars+nonhtvars)
            if newpeq != S.Zero:
                newreducedeqs.append(newpeq)
                hassinglevariable |= any([all([__builtin__.sum(monom)==monom[i] for monom in newpeq.monoms()]) for i in range(3)])
        
        if hassinglevariable:
            log.info('hassinglevariable, trying with raw equations')
            AllEquations = []
            for eq in reducedeqs:
                peq = Poly(eq,tvar)
                if sum(peq.degree_list()) == 0:
                    AllEquations.append(peq.TC().subs(self.invsubs).expand())
                elif sum(peq.degree_list()) == 1 and peq.TC() == S.Zero:
                    AllEquations.append(peq.LC().subs(self.invsubs).expand())
                else:
                    # two substitutions: sin/(1+cos), (1-cos)/sin
                    neweq0 = S.Zero
                    neweq1 = S.Zero
                    for monoms,c in peq.terms():
                        neweq0 += c*(svar**monoms[0])*((1+cvar)**(peq.degree(0)-monoms[0]))
                        neweq1 += c*((1-cvar)**monoms[0])*(svar**(peq.degree(0)-monoms[0]))
                    AllEquations.append(neweq0.subs(self.invsubs).expand())
                    AllEquations.append(neweq1.subs(self.invsubs).expand())
            unusedvars = [solvejointvar for solvejointvar in solvejointvars if not solvejointvar in usedvars]
            for eq in AllEquationsExtra:
                #if eq.has(*usedvars) and not eq.has(*unusedvars):
                AllEquations.append(eq)
            self.sortComplexity(AllEquations)

            # first try to solve all the variables at once
            try:
                solutiontree = self.SolveAllEquations(AllEquations,curvars=solvejointvars,othersolvedvars=self.freejointvars[:], solsubs=self.freevarsubs[:], endbranchtree=endbranchtree, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                return solutiontree, solvejointvars
            except self.CannotSolveError, e:
                log.debug(u'failed solving all variables: %s', e)
                
            try:
                solutiontree = self.SolveAllEquations(AllEquations,curvars=usedvars,othersolvedvars=self.freejointvars[:],solsubs=self.freevarsubs[:], unknownvars=unusedvars, endbranchtree=endbranchtree, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                return solutiontree, usedvars
            except self.CannotSolveError, e:
                log.debug(u'failed solving used variables: %s', e)
            
            for ivar in range(3):
                try:
                    unknownvars = usedvars[:]
                    unknownvars.pop(ivar)
                    endbranchtree2 = []
                    if 1:
                        solutiontree = self.SolveAllEquations(AllEquations,curvars=[usedvars[ivar]],othersolvedvars=self.freejointvars[:],solsubs=self.freevarsubs[:],endbranchtree=[AST.SolverSequence([endbranchtree2])],unknownvars=unknownvars+unusedvars, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                        endbranchtree2 += self.SolveAllEquations(AllEquations,curvars=unknownvars[0:2],othersolvedvars=self.freejointvars[:]+[usedvars[ivar]],solsubs=self.freevarsubs[:]+self.Variable(usedvars[ivar]).subs, unknownvars=unusedvars, endbranchtree=endbranchtree, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                    return preprocesssolutiontree+solutiontree, usedvars#+unusedvars#[unknownvars[1], usedvars[ivar]]#
                except self.CannotSolveError, e:
                    log.debug(u'single variable %s failed: %s', usedvars[ivar], e)
                    
#         try:
#             testvars = [Symbol(othersymbols[0].name[1:]),Symbol(othersymbols[2].name[1:]),Symbol(varname)]
#             AllEquations = [(peq[0].as_expr()-peq[1].as_expr()).expand() for peq in polyeqs if not peq[0].has(*symbols)]
#             coupledsolutions = self.SolveAllEquations(AllEquations,curvars=testvars,othersolvedvars=self.freejointvars[:],solsubs=self.freevarsubs[:],endbranchtree=endbranchtree)
#             return coupledsolutions,testvars
#         except self.CannotSolveError:
#             pass
#
        exportcoeffeqs = None
        # only support ileftvar=0 for now
        for ileftvar in [0]:#range(len(htvars)):
            # always take the equations 4 at a time....?
            if len(newreducedeqs) == 3:
                try:
                    exportcoeffeqs,exportmonoms = self.solveDialytically(newreducedeqs,ileftvar,getsubs=getsubs)
                    break
                except self.CannotSolveError,e:
                    log.warn('failed with leftvar %s: %s',newreducedeqs[0].gens[ileftvar],e)
            else:
                for dialyticeqs in combinations(newreducedeqs,3):
                    try:
                        exportcoeffeqs,exportmonoms = self.solveDialytically(dialyticeqs,ileftvar,getsubs=getsubs)
                        break
                    except self.CannotSolveError,e:
                        log.warn('failed with leftvar %s: %s',newreducedeqs[0].gens[ileftvar],e)
                
                for dialyticeqs in combinations(newreducedeqs,4):
                    try:
                        exportcoeffeqs,exportmonoms = self.solveDialytically(dialyticeqs,ileftvar,getsubs=getsubs)
                        break
                    except self.CannotSolveError,e:
                        log.warn('failed with leftvar %s: %s',newreducedeqs[0].gens[ileftvar],e)

                if exportcoeffeqs is None:
                    filteredeqs = [peq for peq in newreducedeqs if peq.degree() <= 2] # has never worked for higher degrees than 2
                    for dialyticeqs in combinations(filteredeqs,6):
                        try:
                            exportcoeffeqs,exportmonoms = self.solveDialytically(dialyticeqs,ileftvar,getsubs=getsubs)
                            break
                        except self.CannotSolveError,e:
                            log.warn('failed with leftvar %s: %s',newreducedeqs[0].gens[ileftvar],e)
            if exportcoeffeqs is not None:
                break
        
        self._CheckPreemptFn(progress = 0.11)
        if exportcoeffeqs is None:
            if len(nonhtvars) > 0 and newreducedeqs[0].degree:
                log.info('try to solve one variable in terms of the others')
                doloop = True
                while doloop:
                    doloop = False
                    
                    # check if there is an equation that can solve for nonhtvars[0] easily
                    solvenonhtvareq = None
                    for peq in newreducedeqs:
                        if peq.degree(len(htvars)) == 1:
                            solvenonhtvareq = peq
                            break
                    if solvenonhtvareq is None:
                        break
                    
                    # nonhtvars[0] index is len(htvars)
                    usedvar0solution = solve(newreducedeqs[0],nonhtvars[0])[0]
                    num,denom = fraction(usedvar0solution)
                    igenoffset = len(htvars)
                    # substitute all instances of the variable
                    processedequations = []
                    for peq in newreducedeqs[1:]:
                        if self.codeComplexity(peq.as_expr()) > 10000:
                            log.warn('equation too big')
                            continue
                        maxdegree = peq.degree(igenoffset)
                        eqnew = S.Zero
                        for monoms,c in peq.terms():
                            term = c
                            term *= denom**(maxdegree-monoms[igenoffset])
                            term *= num**(monoms[igenoffset])
                            for imonom, monom in enumerate(monoms):
                                if imonom != igenoffset and imonom < len(htvars):
                                    term *= htvars[imonom]**monom
                            eqnew += term
                        try:
                            newpeq = Poly(eqnew,htvars)
                        except PolynomialError, e:
                            # most likel uservar0solution was bad...
                            raise self.CannotSolveError('equation %s cannot be represented as a polynomial'%eqnew)
                        
                        if newpeq != S.Zero:
                            processedequations.append(newpeq)
                    
                    if len(processedequations) == 0:
                        break
                    
                    # check if any variables have degree <= 1 for all equations
                    for ihtvar,htvar in enumerate(htvars):
                        leftoverhtvars = list(htvars)
                        leftoverhtvars.pop(ihtvar)
                        freeequations = []
                        linearequations = []
                        higherequations = []
                        for peq in processedequations:
                            if peq.degree(ihtvar) == 0:
                                freeequations.append(peq)
                            elif peq.degree(ihtvar) == 1:
                                linearequations.append(peq)
                            else:
                                higherequations.append(peq)
                        if len(freeequations) > 0:
                            log.info('found a way to solve this! still need to implement it though...')
                        elif len(linearequations) > 0 and len(leftoverhtvars) == 1:
                            # try substituting one into the other equations Ax = B
                            A = S.Zero
                            B = S.Zero
                            for monoms,c in linearequations[0].terms():
                                term = c
                                for imonom, monom in enumerate(monoms):
                                    if imonom != ihtvar:
                                        term *= htvars[imonom]**monom
                                if monoms[ihtvar] > 0:
                                    A += term
                                else:
                                    B -= term
                            Apoly = Poly(A,leftoverhtvars)
                            Bpoly = Poly(B,leftoverhtvars)
                            singlepolyequations = []
                            useequations = linearequations[1:]
                            if len(useequations) == 0:
                                useequations += higherequations
                            for peq in useequations:
                                complexity = self.codeComplexity(peq.as_expr())
                                if complexity < 2000:
                                    peqnew = Poly(S.Zero,leftoverhtvars)
                                    maxhtvardegree = peq.degree(ihtvar)
                                    for monoms,c in peq.terms():
                                        term = c
                                        for imonom, monom in enumerate(monoms):
                                            if imonom != ihtvar:
                                                term *= htvars[imonom]**monom
                                        termpoly = Poly(term,leftoverhtvars)
                                        peqnew += termpoly * (Bpoly**(monoms[ihtvar]) * Apoly**(maxhtvardegree-monoms[ihtvar]))
                                    singlepolyequations.append(peqnew)
                            if len(singlepolyequations) > 0:
                                jointsol = 2*atan(leftoverhtvars[0])
                                jointname = leftoverhtvars[0].name[2:]
                                firstsolution = AST.SolverPolynomialRoots(jointname=jointname,poly=singlepolyequations[0],jointeval=[jointsol],isHinge=self.IsHinge(jointname))
                                firstsolution.checkforzeros = []
                                firstsolution.postcheckforzeros = []
                                firstsolution.postcheckfornonzeros = []
                                firstsolution.postcheckforrange = []
                                # in Ax=B, if A is 0 and B is non-zero, then equation is invalid
                                # however if both A and B evaluate to 0, then equation is still valid
                                # therefore equation is invalid only if A==0&&B!=0
                                firstsolution.postcheckforNumDenom = [(A.as_expr(), B.as_expr())]
                                firstsolution.AddHalfTanValue = True

                                # actually both A and B can evaluate to zero, in which case we have to use a different method to solve them
                                AllEquations = []
                                for eq in reducedeqs:
                                    if self.codeComplexity(eq) > 500:
                                        continue
                                    peq = Poly(eq, tvar)
                                    if sum(peq.degree_list()) == 0:
                                        AllEquations.append(peq.TC().subs(self.invsubs).expand())
                                    elif sum(peq.degree_list()) == 1 and peq.TC() == S.Zero:
                                        AllEquations.append(peq.LC().subs(self.invsubs).expand())
                                    else:
                                        # two substitutions: sin/(1+cos), (1-cos)/sin
                                        neweq0 = S.Zero
                                        neweq1 = S.Zero
                                        for monoms,c in peq.terms():
                                            neweq0 += c*(svar**monoms[0])*((1+cvar)**(peq.degree(0)-monoms[0]))
                                            neweq1 += c*((1-cvar)**monoms[0])*(svar**(peq.degree(0)-monoms[0]))
                                        if self.codeComplexity(neweq0) > 1000 or self.codeComplexity(neweq1) > 1000:
                                            break
                                        AllEquations.append(neweq0.subs(self.invsubs).expand())
                                        AllEquations.append(neweq1.subs(self.invsubs).expand())

                                #oldmaxcasedepth = self.maxcasedepth                            
                                try:
                                    #self.maxcasedepth = min(self.maxcasedepth, 2)
                                    solvevar = Symbol(jointname)
                                    curvars = list(usedvars)
                                    curvars.remove(solvevar)
                                    unusedvars = [solvejointvar for solvejointvar in solvejointvars if not solvejointvar in usedvars]
                                    solutiontree = self.SolveAllEquations(AllEquations+AllEquationsExtra,curvars=curvars+unusedvars,othersolvedvars=self.freejointvars[:]+[solvevar],solsubs=self.freevarsubs[:]+self.Variable(solvevar).subs,endbranchtree=endbranchtree, canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                                    #secondSolutionComplexity = self.codeComplexity(B) + self.codeComplexity(A)
                                    #if secondSolutionComplexity > 500:
                                    #    log.info('solution for %s is too complex, so delaying its solving')
                                    #solutiontree = self.SolveAllEquations(AllEquations,curvars=curvars,othersolvedvars=self.freejointvars[:]+[solvevar],solsubs=self.freevarsubs[:]+self.Variable(solvevar).subs,endbranchtree=endbranchtree)
                                    return preprocesssolutiontree+[firstsolution]+solutiontree,usedvars+unusedvars

                                except self.CannotSolveError, e:
                                    log.debug('could not solve full variables from scratch, so use existing solution: %s', e)
                                    secondsolution = AST.SolverSolution(htvar.name[2:], isHinge=self.IsHinge(htvar.name[2:]))
                                    secondsolution.jointeval = [2*atan2(B.as_expr(), A.as_expr())]
                                    secondsolution.AddHalfTanValue = True
                                    thirdsolution = AST.SolverSolution(nonhtvars[0].name, isHinge=self.IsHinge(nonhtvars[0].name))
                                    thirdsolution.jointeval = [usedvar0solution]
                                    return preprocesssolutiontree+[firstsolution, secondsolution, thirdsolution]+endbranchtree, usedvars
#                               finally:
#                                   self.maxcasedepth = oldmaxcasedepth
            # try to factor the equations manually
            deg1index = None
            for i in range(len(newreducedeqs)):
                if newreducedeqs[i].degree(2) == 1:
                    if self.codeComplexity(newreducedeqs[i].as_expr()) <= 5000:
                        deg1index = i
                        break
                    else:
                        log.warn('found equation with linear DOF, but too complex so skip')
            if deg1index is not None:
                # try to solve one variable in terms of the others
                if len(htvars) > 2:
                    usedvar0solutions = [solve(newreducedeqs[deg1index],htvars[2])[0]]
                    # check which index in usedvars matches htvars[2]
                    for igenoffset in range(len(usedvars)):
                        if htvars[2].name.find(usedvars[igenoffset].name) >= 0:
                            break
                    polyvars = htvars[0:2]
                elif len(htvars) > 1:
                    usedvar0solutions = solve(newreducedeqs[deg1index],htvars[1])
                    igenoffset = 1
                    polyvars = htvars[0:1] + nonhtvars
                else:
                    usedvar0solutions = []
                processedequations = []
                if len(usedvar0solutions) > 0:
                    usedvar0solution = usedvar0solutions[0]
                    num,denom = fraction(usedvar0solution)
                    # substitute all instances of the variable
                    
                    for ipeq, peq in enumerate(newreducedeqs):
                        if ipeq == deg1index:
                            continue
                        newpeq = S.Zero
                        if peq.degree(igenoffset) > 1:
                            # ignore higher powers
                            continue
                        elif peq.degree(igenoffset) == 0:
                            newpeq = Poly(peq,*polyvars)
                        else:
                            maxdegree = peq.degree(igenoffset)
                            eqnew = S.Zero
                            for monoms,c in peq.terms():
                                term = c*denom**(maxdegree-monoms[igenoffset])
                                term *= num**(monoms[igenoffset])
                                for imonom, monom in enumerate(monoms):
                                    if imonom != igenoffset:
                                        term *= peq.gens[imonom]**monom
                                eqnew += term.expand()
                            try:
                                newpeq = Poly(eqnew,*polyvars)
                            except PolynomialError, e:
                                # most likel uservar0solution was bad
                                raise self.CannotSolveError('equation %s cannot be represented as a polynomial'%eqnew)

                        if newpeq != S.Zero:
                            # normalize by the greatest coefficient in LC, or otherwise determinant will never succeed
                            LC=newpeq.LC()
                            highestcoeff = None
                            if LC.is_Add:
                                for arg in LC.args:
                                    coeff = None
                                    if arg.is_Mul:
                                        coeff = S.One
                                        for subarg in arg.args:
                                            if subarg.is_number:
                                                coeff *= abs(subarg)
                                    elif arg.is_number:
                                        coeff = abs(arg)
                                    if coeff is not None:
                                        if coeff > S.One:
                                            # round to the nearest integer
                                            coeff = int(round(coeff.evalf()))
                                        if highestcoeff is None or coeff > highestcoeff:
                                            highestcoeff = coeff
                            if highestcoeff == oo:
                                log.warn('an equation has inifinity?!')
                            else:
                                if highestcoeff is not None:
                                    processedequations.append(newpeq*(S.One/highestcoeff))
                                else:
                                    processedequations.append(newpeq)
                        else:
                            log.info('equation is zero, so ignoring')
                for dialyticeqs in combinations(processedequations,3):
                    Mall = None
                    leftvar = None
                    for ileftvar in range(2):
                        # TODO, sometimes this works and sometimes this doesn't
                        try:
                            Mall, allmonoms = self.solveDialytically(dialyticeqs,ileftvar,returnmatrix=True)
                            if Mall is not None:
                                leftvar=processedequations[0].gens[ileftvar]
                                break
                        except self.CannotSolveError, e:
                            log.debug(e)
                    if Mall is None:
                        continue
                    log.info('success in solving sub-coeff matrix!')
                    shape=Mall[0].shape
                    Malltemp = [None]*len(Mall)
                    M = zeros(shape)
                    dictequations2 = list(dictequations)
                    for idegree in range(len(Mall)):
                        Malltemp[idegree] = zeros(shape)
                        for i in range(shape[0]):
                            for j in range(shape[1]):
                                if Mall[idegree][i,j] != S.Zero:
                                    sym = self.gsymbolgen.next()
                                    Malltemp[idegree][i,j] = sym
                                    dictequations2.append((sym,Mall[idegree][i,j]))
                        M += Malltemp[idegree]*leftvar**idegree
                    tempsymbols = [self.gsymbolgen.next() for i in range(len(M))]
                    tempsubs = []
                    for i in range(len(tempsymbols)):
                        if M[i] != S.Zero:
                            tempsubs.append((tempsymbols[i],Poly(M[i],leftvar)))
                        else:
                            tempsymbols[i] = S.Zero
                    Mtemp = Matrix(M.shape[0],M.shape[1],tempsymbols)                    
                    dettemp=Mtemp.det()
                    log.info('multiplying all determinant coefficients for solving %s',leftvar)
                    eqadds = []
                    for arg in dettemp.args:
                        eqmuls = [Poly(arg2.subs(tempsubs),leftvar) for arg2 in arg.args]
                        if sum(eqmuls[0].degree_list()) == 0:
                            eq = eqmuls.pop(0)
                            eqmuls[0] = eqmuls[0]*eq
                        while len(eqmuls) > 1:
                            ioffset = 0
                            eqmuls2 = []
                            while ioffset < len(eqmuls)-1:
                                eqmuls2.append(eqmuls[ioffset]*eqmuls[ioffset+1])
                                ioffset += 2
                            eqmuls = eqmuls2
                        eqadds.append(eqmuls[0])
                    det = Poly(S.Zero,leftvar)
                    for eq in eqadds:
                        det += eq
                        
                    jointsol = 2*atan(leftvar)
                    firstsolution = AST.SolverPolynomialRoots(jointname=usedvars[ileftvar].name,poly=det,jointeval=[jointsol],isHinge=self.IsHinge(usedvars[ileftvar].name))
                    firstsolution.checkforzeros = []
                    firstsolution.postcheckforzeros = []
                    firstsolution.postcheckfornonzeros = []
                    firstsolution.postcheckforrange = []
                    firstsolution.dictequations = dictequations2
                    firstsolution.AddHalfTanValue = True
                    
                    # just solve the lowest degree one
                    complexity = [(eq.degree(1-ileftvar)*100000+self.codeComplexity(eq.as_expr()),eq) for eq in processedequations if eq.degree(1-ileftvar) > 0]
                    complexity.sort(key=itemgetter(0))
                    orderedequations = [peq for c,peq in complexity]
                    jointsol = 2*atan(htvars[1-ileftvar])
                    secondsolution = AST.SolverPolynomialRoots(jointname=usedvars[1-ileftvar].name,poly=Poly(orderedequations[0],htvars[1-ileftvar]),jointeval=[jointsol],isHinge=self.IsHinge(usedvars[1-ileftvar].name))
                    secondsolution.checkforzeros = []
                    secondsolution.postcheckforzeros = []
                    secondsolution.postcheckfornonzeros = []
                    secondsolution.postcheckforrange = []
                    secondsolution.AddHalfTanValue = True
                    
                    thirdsolution = AST.SolverSolution(usedvars[2].name, isHinge=self.IsHinge(usedvars[2].name))
                    thirdsolution.jointeval = [usedvar0solution]
                    return preprocesssolutiontree+[firstsolution, secondsolution, thirdsolution]+endbranchtree, usedvars

                    
            raise self.CannotSolveError('failed to solve dialytically')

        if 0:
            # quadratic equations
            iquadvar = 1
            quadpoly0 = Poly(newreducedeqs[0].as_expr(), htvars[iquadvar])
            quadpoly1 = Poly(newreducedeqs[2].as_expr(), htvars[iquadvar])
            a0, b0, c0 = quadpoly0.coeffs()
            a1, b1, c1 = quadpoly1.coeffs()
            quadsolnum = (-a1*c0 + a0*c1).expand()
            quadsoldenom = (-a1*b0 + a0*b1).expand()
            
        if ileftvar > 0:
            raise self.CannotSolveError('solving equations dialytically succeeded with var index %d, unfortunately code generation supports only index 0'%ileftvar)
        
        exportvar = [htvars[ileftvar].name]
        exportvar += [v.name for i,v in enumerate(htvars) if i != ileftvar]
        exportfnname = 'solvedialyticpoly12qep' if len(exportmonoms) == 9 else 'solvedialyticpoly8qep'
        coupledsolution = AST.SolverCoeffFunction(jointnames=[v.name for v in usedvars],jointeval=[v[1] for v in htvarsubs2],jointevalcos=[htvarsubs[2*i][1] for i in range(len(htvars))],jointevalsin=[htvarsubs[2*i+1][1] for i in range(len(htvars))],isHinges=[self.IsHinge(v.name) for v in usedvars],exportvar=exportvar,exportcoeffeqs=exportcoeffeqs,exportfnname=exportfnname, rootmaxdim=16)
        coupledsolution.presetcheckforzeros = checkforzeros
        coupledsolution.dictequations = dictequations
        solutiontree.append(coupledsolution)
        self.usinglapack = True

        if 0:
            if currentcases is None:
                currentcases = set()
            if currentcasesubs is None:
                currentcasesubs = list()
            
            rotsymbols = set(self.Tee[:3,:3])
            possiblesub = [(self.Tee[1,2], S.Zero)]
            possiblesub2 = [(self.Tee[2,2], S.Zero)]
            possiblevar,possiblevalue = possiblesub[0]
            possiblevar2,possiblevalue2 = possiblesub2[0]
            cond = Abs(possiblevar-possiblevalue.evalf(n=30))
            evalcond = Abs(fmod(possiblevar-possiblevalue+pi,2*pi)-pi)# + evalcond
            cond2 = Abs(possiblevar2-possiblevalue2.evalf(n=30))
            evalcond2 = Abs(fmod(possiblevar2-possiblevalue2+pi,2*pi)-pi)# + evalcond
            if self._iktype == 'transform6d' and possiblevar in rotsymbols and possiblevalue == S.Zero and possiblevar2 in rotsymbols and possiblevalue2 == S.Zero:
                checkexpr = [[cond+cond2],evalcond+evalcond2, possiblesub+possiblesub2, []]
                #flatzerosubstitutioneqs.append(checkexpr)
                #localsubstitutioneqs.append(checkexpr)
                #handledconds.append(cond+cond2)
                row1 = int(possiblevar.name[-2])
                col1 = int(possiblevar.name[-1])
                row2 = int(possiblevar2.name[-2])
                col2 = int(possiblevar2.name[-1])
                row3 = 3 - row1 - row2
                col3 = 3 - col1 - col2
                if row1 == row2:
                    # (row1, col3) is either 1 or -1, but don't know which.
                    # know that (row1+1,col3) and (row1+2,col3) are zero though...
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], (row2+1)%3, col3)), S.Zero))
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], (row1+2)%3, col3)), S.Zero))
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row1, col3))**2, S.One)) # squared in the corner should always be 1
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row1, col3))**3, Symbol('%s%d%d'%(possiblevar.name[:-2], row1, col3)))) # squared in the corner should always be 1
                    # furthermore can defer that the left over 4 values are [cos(ang), sin(ang), cos(ang), -sin(ang)] = abcd
                    if row1 == 1:
                        minrow = 0
                        maxrow = 2
                    else:
                        minrow = (row1+1)%3
                        maxrow = (row1+2)%3
                    ra = Symbol('%s%d%d'%(possiblevar.name[:-2], minrow, col1))
                    rb = Symbol('%s%d%d'%(possiblevar.name[:-2], minrow, col2))
                    rc = Symbol('%s%d%d'%(possiblevar.name[:-2], maxrow, col1))
                    rd = Symbol('%s%d%d'%(possiblevar.name[:-2], maxrow, col2))
                    checkexpr[2].append((rb**2, S.One-ra**2))
                    checkexpr[2].append((rb**3, rb-rb*ra**2)) # need 3rd power since sympy cannot divide out the square
                    checkexpr[2].append((rc**2, S.One-ra**2))
                    #checkexpr[2].append((rc, -rb)) # not true
                    #checkexpr[2].append((rd, ra)) # not true
                elif col1 == col2:
                    # (row3, col1) is either 1 or -1, but don't know which.
                    # know that (row3,col1+1) and (row3,col1+2) are zero though...
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row3, (col1+1)%3)), S.Zero))
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row3, (col1+2)%3)), S.Zero))
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row3, col1))**2, S.One)) # squared in the corner should always be 1
                    checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row3, col1))**3, Symbol('%s%d%d'%(possiblevar.name[:-2], row3, col1)))) # squared in the corner should always be 1
                    # furthermore can defer that the left over 4 values are [cos(ang), sin(ang), cos(ang), -sin(ang)] = abcd
                    if col1 == 1:
                        mincol = 0
                        maxcol = 2
                    else:
                        mincol = (col1+1)%3
                        maxcol = (col1+2)%3
                    ra = Symbol('%s%d%d'%(possiblevar.name[:-2], row1, mincol))
                    rb = Symbol('%s%d%d'%(possiblevar.name[:-2], row2, mincol))
                    rc = Symbol('%s%d%d'%(possiblevar.name[:-2], row1, maxcol))
                    rd = Symbol('%s%d%d'%(possiblevar.name[:-2], row2, maxcol))
                    checkexpr[2].append((rb**2, S.One-ra**2))
                    checkexpr[2].append((rb**3, rb-rb*ra**2)) # need 3rd power since sympy cannot divide out the square
                    checkexpr[2].append((rc**2, S.One-ra**2))
                    #checkexpr[2].append((rc, -rb)) # not true
                    #checkexpr[2].append((rd, ra)) # not true


        return preprocesssolutiontree+solutiontree+endbranchtree,usedvars

    def ConvertSinCosEquationToHalfTan(self, eq, convertvars):
        """converts all the sin/cos of variables to half-tangents. Returns two equations (poly, denominator)
        """
        cossinvars = []
        htvarsubs = []
        htvars = []
        htvarsubsinv = []
        cossinsubs = []
        for varsym in convertvars:
            var = self.Variable(varsym)
            cossinvars.append(var.cvar)
            cossinvars.append(var.svar)
            htvar = Symbol('ht%s'%varsym.name)
            htvarsubs += [(var.cvar,(1-htvar**2)/(1+htvar**2)),(var.svar,2*htvar/(1+htvar**2))]
            htvarsubsinv.append((htvar, (1-var.cvar)/var.svar))
            htvars.append(htvar)
            cossinsubs.append((cos(varsym), var.cvar))
            cossinsubs.append((sin(varsym), var.svar))
        peq = Poly(eq.subs(cossinsubs),*cossinvars)
        maxdenom = [0]*len(convertvars)
        for monoms in peq.monoms():
            for i in range(len(convertvars)):
                maxdenom[i] = max(maxdenom[i],monoms[2*i]+monoms[2*i+1])
        eqnew = S.Zero
        for monoms,c in peq.terms():
            term = c
            for i in range(len(convertvars)):
                # for cos
                num, denom = fraction(htvarsubs[2*i][1])
                term *= num**monoms[2*i]
                # for sin
                num, denom = fraction(htvarsubs[2*i+1][1])
                term *= num**monoms[2*i+1]
            # the denoms for sin/cos of the same joint variable are the same
            for i in range(len(convertvars)):
                denom = fraction(htvarsubs[2*i][1])[1]
                exp = maxdenom[i] - monoms[2*i] - monoms[2*i+1]
                if exp > 0:
                    term *= denom**exp
            eqnew += term
        #newpeq = Poly(eqnew,htvars)
        othereq = S.One
        for i in range(len(convertvars)):
            othereq *= (1+htvars[i]**2)**maxdenom[i]
        return eqnew, othereq, htvarsubsinv

    def ConvertHalfTanEquationToSinCos(self, eq, convertvars):
        """converts all the sin/cos of variables to half-tangents. Returns two equations (poly, denominator)
        """
        assert(0)
        cossinvars = []
        htvarsubs = []
        htvars = []
        htvarsubsinv = []
        for varsym in convertvars:
            var = self.Variable(varsym) 
            cossinvars.append(var.cvar)
            cossinvars.append(var.svar)
            htvar = Symbol('ht%s'%varsym.name)
            htvarsubs += [(var.cvar,(1-htvar**2)/(1+htvar**2)),(var.svar,2*htvar/(1+htvar**2))]
            htvarsubsinv.append((htvar, (1-var.cvar)/var.svar))
            htvars.append(htvar)
        peq = Poly(eq,*cossinvars)
        maxdenom = [0]*len(convertvars)
        for monoms in peq.monoms():
            for i in range(len(convertvars)):
                maxdenom[i] = max(maxdenom[i],monoms[2*i]+monoms[2*i+1])
        eqnew = S.Zero
        for monoms,c in peq.terms():
            term = c
            for i in range(len(convertvars)):
                # for cos
                num, denom = fraction(htvarsubs[2*i][1])
                term *= num**monoms[2*i]
                # for sin
                num, denom = fraction(htvarsubs[2*i+1][1])
                term *= num**monoms[2*i+1]
            # the denoms for sin/cos of the same joint variable are the same
            for i in range(len(convertvars)):
                denom = fraction(htvarsubs[2*i][1])[1]
                exp = maxdenom[i] - monoms[2*i] - monoms[2*i+1]
                if exp > 0:
                    term *= denom**exp
            eqnew += term
        #newpeq = Poly(eqnew,htvars)
        othereq = S.One
        for i in range(len(convertvars)):
            othereq *= (1+htvars[i]**2)**maxdenom[i]
        return eqnew, othereq, htvarsubsinv
    
    def solveKohliOsvatic(self,rawpolyeqs,solvejointvars,endbranchtree, AllEquationsExtra=None, currentcases=None, currentcasesubs=None):
        """Find a 16x16 matrix where the entries are linear with respect to the tan half-angle of one of the variables [Kohli1993]_. Takes in the 14 raghavan/roth equations.
        
        .. [Kohli1993] Dilip Kohli and M. Osvatic, "Inverse Kinematics of General 6R and 5R,P Serial Manipulators", Journal of Mechanical Design, Volume 115, Issue 4, Dec 1993.
        """
        log.info('attempting kohli/osvatic general ik method')
        if len(rawpolyeqs[0][0].gens) < len(rawpolyeqs[0][1].gens):
            for peq in rawpolyeqs:
                peq[0],peq[1] = peq[1],peq[0]

        symbols = list(rawpolyeqs[0][0].gens)
        othersymbols = list(rawpolyeqs[0][1].gens)
        othersymbolsnames = []
        for s in othersymbols:
            testeq = s.subs(self.invsubs)
            for solvejointvar in solvejointvars:
                if testeq.has(solvejointvar):
                    othersymbolsnames.append(solvejointvar)
                    break
        assert(len(othersymbols)==len(othersymbolsnames))
        symbolsubs = [(symbols[i].subs(self.invsubs),symbols[i]) for i in range(len(symbols))]
        if len(symbols) != 6:
            raise self.CannotSolveError('Kohli/Osvatic method requires 3 unknown variables')
            
        # choose which leftvar can determine the singularity of the following equations!
        for i in range(0,6,2):
            eqs = [peq for peq in rawpolyeqs if peq[0].has(symbols[i],symbols[i+1])]
            if len(eqs) <= 8:
                break
        if len(eqs) > 8:
            raise self.CannotSolveError('need 8 or less equations of one variable, currently have %d'%len(eqs))
        
        cvar = symbols[i]
        svar = symbols[i+1]
        tvar = Symbol('t'+cvar.name[1:])
        symbols.remove(cvar)
        symbols.remove(svar)
        othereqs = [peq for peq in rawpolyeqs if not peq[0].has(cvar,svar)]

        polyeqs = [[eq[0].as_expr(),eq[1]] for eq in eqs]
        if len(polyeqs) < 8:
            raise self.CannotSolveError('solveKohliOsvatic: need 8 or more polyeqs')

        # solve the othereqs for symbols without the standalone symbols[2] and symbols[3]
        reducedeqs = []
        othersymbolsnamesunique = list(set(othersymbolsnames)) # get the unique names
        for jother in range(len(othersymbolsnamesunique)):
            if not self.IsHinge(othersymbolsnamesunique[jother].name):
                continue
            othervar=self.Variable(othersymbolsnamesunique[jother])
            cosmonom = [0]*len(othersymbols)
            cosmonom[othersymbols.index(othervar.cvar)] = 1
            cosmonom = tuple(cosmonom)
            sinmonom = [0]*len(othersymbols)
            sinmonom[othersymbols.index(othervar.svar)] = 1
            sinmonom = tuple(sinmonom)
            leftsideeqs = []
            rightsideeqs = []
            finaleqsymbols = symbols + [othervar.cvar,othervar.svar]
            for eq0,eq1 in othereqs:
                leftsideeq = Poly(eq1,*othersymbols)
                leftsideeqdict = leftsideeq.as_dict()
                rightsideeq = Poly(eq0,*finaleqsymbols)
                coscoeff = leftsideeqdict.get(cosmonom,S.Zero)
                if coscoeff != S.Zero:
                    rightsideeq = rightsideeq - othervar.cvar*coscoeff
                    leftsideeq = leftsideeq - othervar.cvar*coscoeff
                sincoeff = leftsideeqdict.get(sinmonom,S.Zero)
                if sincoeff != S.Zero:
                    rightsideeq = rightsideeq - othervar.svar*sincoeff
                    leftsideeq = leftsideeq - othervar.svar*sincoeff
                const = leftsideeq.TC()
                if const != S.Zero:
                    rightsideeq = rightsideeq - const
                    leftsideeq = leftsideeq - const
                # check that leftsideeq doesn't hold any terms with cosmonom and sinmonom?
                rightsideeqs.append(rightsideeq)
                leftsideeqs.append(leftsideeq)
            # number of symbols for kawada-hiro robot is 16
            if len(othersymbols) > 2:
                reducedeqs = self.reduceBothSidesSymbolically(leftsideeqs,rightsideeqs,usesymbols=False,maxsymbols=18)
                for peq in reducedeqs:
                    peq[0] = Poly(peq[0],*othersymbols)
            else:
                reducedeqs = [[left,right] for left,right in izip(leftsideeqs,rightsideeqs)]
            if len(reducedeqs) > 0:
                break
            
        if len(reducedeqs) == 0:
            raise self.CannotSolveError('KohliOsvatic method: could not reduce the equations')

        finaleqs = []
        for peq0,eq1 in reducedeqs:
            if peq0 == S.Zero:
                finaleqs.append(Poly(eq1,*finaleqsymbols))

        if len(finaleqs) >= 2:
            # perhaps can solve finaleqs as is?
            # transfer othersymbols[2*jother:(2+2*jother)] to the leftside
            try:
                leftsideeqs = []
                rightsideeqs = []
                for finaleq in finaleqs:
                    peq=Poly(finaleq,*othersymbols[2*jother:(2+2*jother)])
                    leftsideeqs.append(peq.sub(peq.TC()))
                    rightsideeqs.append(-peq.TC())
                reducedeqs2 = self.reduceBothSidesSymbolically(leftsideeqs,rightsideeqs,usesymbols=False,maxsymbols=18)
                # find all the equations with left side = to zero
                usedvars = set()
                for symbol in symbols:
                    usedvars.add(Symbol(symbol.name[1:]))
                AllEquations = []
                for eq0, eq1 in reducedeqs2:
                    if eq0 == S.Zero:
                        AllEquations.append(eq1.subs(self.invsubs))
                if len(AllEquations) > 0:
                    otherjointtrees = []
                    tree = self.SolveAllEquations(AllEquations,curvars=list(usedvars),othersolvedvars=[],solsubs=self.freevarsubs,endbranchtree=[AST.SolverSequence([otherjointtrees])], canguessvars=False, currentcases=currentcases, currentcasesubs=currentcasesubs)
                    log.info('first SolveAllEquations successful: %s',usedvars)
#                     try:
#                         # although things can be solved at this point, it yields a less optimal solution than if all variables were considered...
#                         solsubs=list(self.freevarsubs)
#                         for usedvar in usedvars:
#                             solsubs += self.Variable(usedvar).subs
#                         # solved, so substitute back into reducedeqs and see if anything new can be solved
#                         otherusedvars = set()
#                         for symbol in othersymbols:
#                             otherusedvars.add(Symbol(symbol.name[1:]))
#                         OtherAllEquations = []
#                         for peq0,eq1 in reducedeqs:
#                             OtherAllEquations.append((peq0.as_expr()-eq1).subs(self.invsubs).expand())
#                         otherjointtrees += self.SolveAllEquations(OtherAllEquations,curvars=list(otherusedvars),othersolvedvars=list(usedvars),solsubs=solsubs,endbranchtree=endbranchtree)
#                         return tree, list(usedvars)+list(otherusedvars)
#                     except self.CannotSolveError:
                        # still have the initial solution
                    otherjointtrees += endbranchtree
                    return tree, list(usedvars)
                
            except self.CannotSolveError,e:
                pass
        
        log.info('build final equations for symbols: %s',finaleqsymbols)
        neweqs=[]
        for i in range(0,8,2):
            p0 = Poly(polyeqs[i][0],cvar,svar)
            p0dict = p0.as_dict()
            p1 = Poly(polyeqs[i+1][0],cvar,svar)
            p1dict = p1.as_dict()
            r0 = polyeqs[i][1].as_expr()
            r1 = polyeqs[i+1][1].as_expr()
            if self.equal(p0dict.get((1,0),S.Zero),-p1dict.get((0,1),S.Zero)) and self.equal(p0dict.get((0,1),S.Zero),p1dict.get((1,0),S.Zero)):
                p0,p1 = p1,p0
                p0dict,p1dict=p1dict,p0dict
                r0,r1 = r1,r0
            if self.equal(p0dict.get((1,0),S.Zero),p1dict.get((0,1),S.Zero)) and self.equal(p0dict.get((0,1),S.Zero),-p1dict.get((1,0),S.Zero)):
                # p0+tvar*p1, p1-tvar*p0
                # subs: tvar*svar + cvar = 1, svar-tvar*cvar=tvar
                neweqs.append([Poly(p0dict.get((1,0),S.Zero) + p0dict.get((0,1),S.Zero)*tvar + p0.TC() + tvar*p1.TC(),*symbols), Poly(r0+tvar*r1,*othersymbols)])
                neweqs.append([Poly(p0dict.get((1,0),S.Zero)*tvar - p0dict.get((0,1),S.Zero) - p0.TC()*tvar + p1.TC(),*symbols), Poly(r1-tvar*r0,*othersymbols)])
        if len(neweqs) != 8:
            raise self.CannotSolveError('coefficients of equations need to match! only got %d reduced equations'%len(neweqs))
    
        for eq0,eq1 in neweqs:
            commondenom = Poly(S.One,*self.pvars)
            hasunknown = False
            for m,c in eq1.terms():
                foundreq = [req[1] for req in reducedeqs if req[0].monoms()[0] == m]
                if len(foundreq) > 0:
                    n,d = fraction(foundreq[0])
                    commondenom = Poly(lcm(commondenom,d),*self.pvars)
                else:
                    if m[2*(1-jother)] > 0 or m[2*(1-jother)+1] > 0:
                        # perhaps there's a way to combine what's in reducedeqs?
                        log.warn('unknown %s',m)
                        hasunknown = True
            if hasunknown:
                continue
            commondenom = self.removecommonexprs(commondenom.as_expr(),onlygcd=True,onlynumbers=True)
            finaleq = eq0.as_expr()*commondenom
            for m,c in eq1.terms():
                foundreq = [req[1] for req in reducedeqs if req[0].monoms()[0] == m]
                if len(foundreq) > 0:
                    finaleq = finaleq - c*simplify(foundreq[0]*commondenom)
                else:
                    finaleq = finaleq - Poly.from_dict({m:c*commondenom},*eq1.gens).as_expr()
            finaleqs.append(Poly(finaleq.expand(),*finaleqsymbols))
                
        # finally do the half angle substitution with symbols
        # set:
        # j=othersymbols[2]*(1+dummys[0]**2)*(1+dummys[1]**2)
        # k=othersymbols[3]*(1+dummys[0]**2)*(1+dummys[1]**2)
        dummys = []
        dummysubs = []
        dummysubs2 = []
        dummyvars = []
        usedvars = []

        dummys.append(tvar)
        dummyvars.append((tvar,tan(0.5*Symbol(tvar.name[1:]))))
        usedvars.append(Symbol(cvar.name[1:]))
        dummysubs2.append((usedvars[-1],2*atan(tvar)))
        dummysubs += [(cvar,(1-tvar**2)/(1+tvar**2)),(svar,2*tvar/(1+tvar**2))]

        for i in range(0,len(symbols),2):
            dummy = Symbol('ht%s'%symbols[i].name[1:])
            # [0] - cos, [1] - sin
            dummys.append(dummy)
            dummysubs += [(symbols[i],(1-dummy**2)/(1+dummy**2)),(symbols[i+1],2*dummy/(1+dummy**2))]
            var = symbols[i].subs(self.invsubs).args[0]
            dummyvars.append((dummy,tan(0.5*var)))
            dummysubs2.append((var,2*atan(dummy)))
            if not var in usedvars:
                usedvars.append(var)
        commonmult = (1+dummys[1]**2)*(1+dummys[2]**2)

        usedvars.append(Symbol(othersymbols[2*jother].name[1:]))
        dummyj = Symbol('dummyj')
        dummyk = Symbol('dummyk')
        dummyjk = Symbol('dummyjk')

        dummys.append(dummyj)
        dummyvars.append((dummyj,othersymbols[2*jother]*(1+dummyvars[1][1]**2)*(1+dummyvars[2][1]**2)))
        dummysubs.append((othersymbols[2*jother],cos(dummyjk)))        
        dummys.append(dummyk)
        dummyvars.append((dummyk,othersymbols[1+2*jother]*(1+dummyvars[1][1]**2)*(1+dummyvars[2][1]**2)))
        dummysubs.append((othersymbols[1+2*jother],sin(dummyjk)))
        dummysubs2.append((usedvars[-1],dummyjk))

        newreducedeqs = []
        for peq in finaleqs:
            eqnew = S.Zero
            for monoms,c in peq.terms():
                term = S.One
                for i in range(4):
                    term *= dummysubs[i+2][1]**monoms[i]
                if monoms[4] == 1:
                    eqnew += c * dummyj
                elif monoms[5] == 1:
                    eqnew += c * dummyk
                else:
                    eqnew += c*simplify(term*commonmult)
            newreducedeqs.append(Poly(eqnew,*dummys))

        exportcoeffeqs = None
        for ileftvar in range(len(dummys)):
            leftvar = dummys[ileftvar]
            try:
                exportcoeffeqs,exportmonoms = self.solveDialytically(newreducedeqs,ileftvar,getsubs=None)
                break
            except self.CannotSolveError,e:
                log.warn('failed with leftvar %s: %s',leftvar,e)

        if exportcoeffeqs is None:
            raise self.CannotSolveError('failed to solve dialytically')
        if ileftvar > 0:
            raise self.CannotSolveError('solving equations dialytically succeeded with var index %d, unfortunately code generation supports only index 0'%ileftvar)
    
        coupledsolution = AST.SolverCoeffFunction(jointnames=[v.name for v in usedvars],jointeval=[v[1] for v in dummysubs2],jointevalcos=[dummysubs[2*i][1] for i in range(len(usedvars))],jointevalsin=[dummysubs[2*i+1][1] for i in range(len(usedvars))],isHinges=[self.IsHinge(v.name) for v in usedvars],exportvar=dummys[0:3]+[dummyjk],exportcoeffeqs=exportcoeffeqs,exportfnname='solvedialyticpoly16lep',rootmaxdim=16)
        self.usinglapack = True
        return [coupledsolution]+endbranchtree,usedvars

    def solveDialytically(self,dialyticeqs,ileftvar,returnmatrix=False,getsubs=None):
        """ Return the coefficients to solve equations dialytically (Salmon 1885) leaving out variable index ileftvar.

        Extract the coefficients of 1, leftvar**1, leftvar**2, ... of every equation
        every len(dialyticeqs)*len(monoms) coefficients specify one degree of all the equations (order of monoms is specified in exportmonomorder
        there should be len(dialyticeqs)*len(monoms)*maxdegree coefficients

        Method also checks if the equations are linearly dependent
        """
        self._CheckPreemptFn(progress = 0.12)
        if len(dialyticeqs) == 0:
            raise self.CannotSolveError('solveDialytically given zero equations')
        
        allmonoms = set()
        origmonoms = set()
        maxdegree = 0
        leftvar = dialyticeqs[0].gens[ileftvar]
        extradialyticeqs = []
        for peq in dialyticeqs:
            if sum(peq.degree_list()) == 0:
                log.warn('solveDialytically: polynomial %s degree is 0',peq)
                continue
            for m in peq.monoms():
                mlist = list(m)
                maxdegree=max(maxdegree,mlist.pop(ileftvar))
                allmonoms.add(tuple(mlist))
                origmonoms.add(tuple(mlist))
                mlist[0] += 1
                allmonoms.add(tuple(mlist))
            
            # check if any monoms are not expressed in this poly, and if so, add another poly with the monom multiplied, will this give bad solutions?
            for igen in range(len(peq.gens)):
                if all([m[igen]==0 for m in peq.monoms()]):
                    log.debug('adding extra equation multiplied by %s', peq.gens[igen])
                    extradialyticeqs.append(peq*peq.gens[igen])
                    # multiply by peq.gens[igen]
                    for m in peq.monoms():
                        mlist = list(m)
                        mlist[igen] += 1
                        maxdegree=max(maxdegree,mlist.pop(ileftvar))
                        allmonoms.add(tuple(mlist))
                        origmonoms.add(tuple(mlist))
                        mlist[0] += 1
                        allmonoms.add(tuple(mlist))
        
        dialyticeqs = list(dialyticeqs) + extradialyticeqs # dialyticeqs could be a tuple
        allmonoms = list(allmonoms)
        allmonoms.sort()
        origmonoms = list(origmonoms)
        origmonoms.sort()
        if len(allmonoms)<2*len(dialyticeqs):
            log.warn('solveDialytically equations %d > %d, should be equal...', 2*len(dialyticeqs),len(allmonoms))
            # TODO not sure how to select the equations
            N = len(allmonoms)/2
            dialyticeqs = dialyticeqs[:N]
        if len(allmonoms) == 0 or len(allmonoms)>2*len(dialyticeqs):
            raise self.CannotSolveError('solveDialytically: more unknowns than equations %d>%d'%(len(allmonoms), 2*len(dialyticeqs)))
        
        Mall = [zeros((2*len(dialyticeqs),len(allmonoms))) for i in range(maxdegree+1)]
        Mallindices = [-ones((2*len(dialyticeqs),len(allmonoms))) for i in range(maxdegree+1)]
        exportcoeffeqs = [S.Zero]*(len(dialyticeqs)*len(origmonoms)*(maxdegree+1))
        for ipeq,peq in enumerate(dialyticeqs):
            for m,c in peq.terms():
                mlist = list(m)
                degree=mlist.pop(ileftvar)
                exportindex = degree*len(origmonoms)*len(dialyticeqs) + len(origmonoms)*ipeq+origmonoms.index(tuple(mlist))
                assert(exportcoeffeqs[exportindex] == S.Zero)
                exportcoeffeqs[exportindex] = c
                Mall[degree][len(dialyticeqs)+ipeq,allmonoms.index(tuple(mlist))] = c
                Mallindices[degree][len(dialyticeqs)+ipeq,allmonoms.index(tuple(mlist))] = exportindex
                mlist[0] += 1
                Mall[degree][ipeq,allmonoms.index(tuple(mlist))] = c
                Mallindices[degree][ipeq,allmonoms.index(tuple(mlist))] = exportindex

            # check if any monoms are not expressed in this poly, and if so, add another poly with the monom multiplied, will this give bad solutions?
            for igen in range(len(peq.gens)):
                if all([m[igen]==0 for m in peq.monoms()]):
                    for m,c in peq.terms():
                        mlist = list(m)
                        mlist[igen] += 1
                        degree=mlist.pop(ileftvar)
                        exportindex = degree*len(origmonoms)*len(dialyticeqs) + len(origmonoms)*ipeq+origmonoms.index(tuple(mlist))
                        assert(exportcoeffeqs[exportindex] == S.Zero)
                        exportcoeffeqs[exportindex] = c
                        Mall[degree][len(dialyticeqs)+ipeq,allmonoms.index(tuple(mlist))] = c
                        Mallindices[degree][len(dialyticeqs)+ipeq,allmonoms.index(tuple(mlist))] = exportindex
                        mlist[0] += 1
                        Mall[degree][ipeq,allmonoms.index(tuple(mlist))] = c
                        Mallindices[degree][ipeq,allmonoms.index(tuple(mlist))] = exportindex

        # have to check that the determinant is not zero for several values of ileftvar! It is very common that
        # some equations are linearly dependent and not solvable through this method.
        if self.testconsistentvalues is not None:
            linearlyindependent = False
            for itest,subs in enumerate(self.testconsistentvalues):
                if getsubs is not None:
                    # have to explicitly evaluate since testsubs can be very complex
                    subsvals = [(s,v.evalf()) for s,v in subs]
                    try:
                        subs = subsvals+getsubs(subsvals)
                    except self.CannotSolveError, e:
                        # getsubs failed (sometimes it requires solving inverse matrix), so go to next set
                        continue
                # have to sub at least twice with the global symbols
                A = Mall[maxdegree].subs(subs)
                for i in range(A.shape[0]):
                    for j in range(A.shape[1]):
                        A[i,j] = self._SubstituteGlobalSymbols(A[i,j]).subs(subs).evalf()
                eps = 10**-(self.precision-3)
                try:
                    Anumpy = numpy.array(numpy.array(A), numpy.float64)
                except ValueError, e:
                    log.warn(u'could not convert to numpy array: %s', e)
                    continue
                
                if numpy.isnan(numpy.sum(Anumpy)):
                    log.info('A has NaNs')
                    break
                eigenvals = numpy.linalg.eigvals(Anumpy)
                if all([Abs(f) > eps for f in eigenvals]):
                    try:
                        Ainv = A.inv(method='LU')
                    except ValueError, e:
                        log.error('error when taking inverse: %s', e)
                        continue
                    B = Ainv*Mall[1].subs(subs)
                    for i in range(B.shape[0]):
                        for j in range(B.shape[1]):
                            B[i,j] = self._SubstituteGlobalSymbols(B[i,j]).subs(subs).evalf()
                    C = Ainv*Mall[0].subs(subs).evalf()
                    for i in range(C.shape[0]):
                        for j in range(C.shape[1]):
                            C[i,j] = self._SubstituteGlobalSymbols(C[i,j]).subs(subs).evalf()
                    A2 = zeros((B.shape[0],B.shape[0]*2))
                    for i in range(B.shape[0]):
                        A2[i,B.shape[0]+i] = S.One
                    A2=A2.col_join((-C).row_join(-B))
                    eigenvals2,eigenvecs2 = numpy.linalg.eig(numpy.array(numpy.array(A2),numpy.float64))
                    # check if solutions can actually be extracted
                    # find all the zero eigenvalues
                    roots = []
                    numrepeating = 0
                    for ieig,eigenvalue in enumerate(eigenvals2):
                        if abs(numpy.imag(eigenvalue)) < 1e-12:
                            if abs(numpy.real(eigenvalue)) > 1:
                                ev = eigenvecs2[A.shape[0]:,ieig]
                            else:
                                ev = eigenvecs2[:A.shape[0],ieig]
                            if abs(ev[0]) < 1e-14:
                                continue
                            br = ev[1:] / ev[0]
                            dists = abs(numpy.array(roots) - numpy.real(eigenvalue))
                            if any(dists<1e-7):
                                numrepeating += 1
                            roots.append(numpy.real(eigenvalue))
                    if numrepeating > 0:
                        log.info('found %d repeating roots in solveDialytically matrix: %s',numrepeating,roots)
                        # should go on even if there's repeating roots?
                        continue
                    Atotal = None
                    for idegree in range(maxdegree+1):
                        Adegree = Mall[idegree].subs(subs)
                        for i in range(Adegree.shape[0]):
                            for j in range(Adegree.shape[1]):
                                Adegree[i,j] = self._SubstituteGlobalSymbols(Adegree[i,j]).subs(subs).evalf()
                        if Atotal is None:
                            Atotal = Adegree
                        else:
                            Atotal += Adegree*leftvar**idegree
                    # make sure the determinant of Atotal is not-zero for at least several solutions
                    leftvarvalue = leftvar.subs(subs).evalf()
                    hasnonzerodet = False
                    for testvalue in [-10*S.One, -S.One,-0.5*S.One, 0.5*S.One, S.One, 10*S.One]:
                        Atotal2 = Atotal.subs(leftvar,leftvarvalue+testvalue).evalf()
                        detvalue = Atotal2.det()
                        if abs(detvalue) > 1e-10:
                            hasnonzerodet = True
                    if not hasnonzerodet:
                        log.warn('has zero det, so failed')
                    else:
                        linearlyindependent = True
                    break
                else:
                    log.info('not all abs(eigenvalues) > %e. min is %e', eps, min([Abs(f) for f in eigenvals if Abs(f) < eps]))
            if not linearlyindependent:
                raise self.CannotSolveError('equations are not linearly independent')

        if returnmatrix:
            return Mall,allmonoms

        return exportcoeffeqs,origmonoms

    def SubstituteGinacEquations(self,dictequations, valuesubs, localsymbolmap):
        gvaluesubs = []
        for var, value in valuesubs:
            if value != oo:
                if var.name in localsymbolmap:
                    gvaluesubs.append(localsymbolmap[var.name] == GinacUtils.ConvertToGinac(value,localsymbolmap))
        retvalues = []
        for var, value in dictequations:
            newvalue = value.subs(gvaluesubs).evalf()
            if var.name in localsymbolmap:
                gvaluesubs.append(localsymbolmap[var.name]==newvalue)
            else:
                log.warn('%s not in map',var)
            retvalues.append((var,newvalue))
        return retvalues
    
    def SimplifyTransformPoly(self, peq):
        """
        Simplifies the coefficients of the polynomial with simplifyTransform and returns the new polynomial
        """
        if peq == S.Zero:
            return peq
        
        return peq.termwise(lambda m,c: self.SimplifyTransform(c))
    
    def SimplifyTransform(self, eq, othervars = None):
        """
        Attemps to simplify eq using properties of a 3D rotation matrix, i.e., 18 constraints:

        - 2-norm of each row/column is 1
        - dot product of every pair of rows/columns is 0
        - cross product of every pair (1,2),(2,3),(3,1) of rows/columns is the remaining row/column
        
        othervars (optional list) contains unknown variables with respect to which we simplify eq

        """
        if othervars is not None:
            peq = Poly(eq, *othervars)
            if peq == S.Zero:
                return S.Zero
            
            peqnew = peq.termwise(lambda m, c: self.SimplifyTransform(c))
            return peqnew.as_expr()
        
        # there can be global substitutions like pz = 0.
        # get those that do not start with gconst
        transformsubstitutions = [(var, value) for var, value in self.globalsymbols \
                                  if var.is_Symbol and not var.name.startswith('gconst')]

#        exec(ipython_str) in globals(), locals()
        
        if self._iktype == 'translationdirection5d':
            # since this IK type includes direction, we use the first row only,
            # i.e., r00**2 + r01**2 + r02**2 = 1
            simpiter = 0
            origeq = eq

            # first simplify just rotations since they don't add any new variables
            changed = True
            while changed and eq.has(*self._rotsymbols):
                # log.info('simpiter = %d, complexity = %d', \
                #          simpiter, \
                #          self.codeComplexity(eq.as_expr() if isinstance(eq,Poly) else eq))
                simpiter += 1
                changed = False
                neweq = self._SimplifyRotationNorm(eq, self._rotnormgroups[0:1]) # first row
                if neweq is not None:
                    eq2 = self._SubstituteGlobalSymbols(neweq, transformsubstitutions)
                    if not self.equal(eq, eq2):
                        eq = eq2
                        changed = True
            if isinstance(eq, Poly):
                eq = eq.as_expr()
        
        elif self._iktype == 'transform6d':

            # TO-DO: if there is a divide by self._rotsymbols, then cannot proceed since cannot make Polynomials from them
            if eq.is_Add:
                if any([fraction(arg)[1].has(*self._rotsymbols) for arg in eq.args]):
                    log.info('argument in equation %s has _rotsymbols in its denom; skip', arg)
                    return eq
            elif fraction(eq)[1].has(*self._rotsymbols):
                log.info('equation %s has _rotsymbols in its denom; skip', eq)
                return eq
        
            simpiter = 0
            origeq = eq

            
            # first simplify just rotations since they don't introduce new symbols
            """
            changed = True
            while changed and eq.has(*self._rotsymbols):
                # log.info('simpiter = %d, complexity = %d', \
                #          simpiter, \
                #          self.codeComplexity(eq.as_expr() if isinstance(eq,Poly) else eq))
                simpiter += 1
                changed = False
            
                neweq = self._SimplifyRotationNorm(eq, self._rotsymbols, self._rotnormgroups)
                if neweq is not None:
                    neweq = self._SubstituteGlobalSymbols(neweq, transformsubstitutions)
                    if not self.equal(eq, neweq):
                        eq = neweq
                        changed = True
                    
                neweq = self._SimplifyRotationDot(eq, self._rotsymbols, self._rotdotgroups)
                if neweq is not None:
                    neweq = self._SubstituteGlobalSymbols(neweq, transformsubstitutions)
                    if not self.equal(eq, neweq):
                        eq = neweq
                        changed = True
                    
                neweq = self._SimplifyRotationCross(eq, self._rotsymbols, self._rotcrossgroups)
                if neweq is not None:
                    neweq = self._SubstituteGlobalSymbols(neweq, transformsubstitutions)
                    if not self.equal(eq, neweq):
                        eq = neweq
                        changed = True
            """
            def _SimplifyRotationFcn(fcn, eq, changed, groups):
                neweq = fcn(eq, self._rotpossymbols, groups)
                if neweq is None:
                    return eq, changed
                else:
                    neweq = self._SubstituteGlobalSymbols(neweq, transformsubstitutions)
                    if not self.equal(eq, neweq):
                        changed = True
                    return neweq, changed

            # TGN: no need to check if self.pp is not None, i.e. if full 3D position is available
            changed = True
            while changed and eq.has(*self._rotpossymbols):
                changed = False
                eq, changed = _SimplifyRotationFcn(self._SimplifyRotationNorm , eq, changed, self._rotposnormgroups)
                eq, changed = _SimplifyRotationFcn(self._SimplifyRotationDot  , eq, changed, self._rotposdotgroups)
                eq, changed = _SimplifyRotationFcn(self._SimplifyRotationCross, eq, changed, self._rotposcrossgroups)
                    
            if isinstance(eq, Poly):
                eq = eq.as_expr()
            #log.info("simplify eq:\n%r\n->new eq:\n%r", origeq, eq)
        else:
            # not translationdirection5d nor transform6d
            pass

        return eq

    
    def _SimplifyRotationNorm(self, eq, symbols, groups):
        """
        Simplify eq based on 2-norm of each row/column being 1

        symbols is self._rotsymbols   or self._rotpossymbols
        groups is self._rotnormgroups or self._rotposnormgroups

        Called by SimplifyTransform only.
        """

        neweq = None
        for group in groups:
            try:
                # not sure about this thresh
                if self.codeComplexity(eq) > 300:
                    log.warn(u'equation too complex to simplify for rot norm: %s', eq)
                    continue

                # exec(ipython_str)
                
                # need to do 1234*group[3] hack in order to get the Poly domain to recognize group[3] (sympy 0.7.1)
                # p = Poly(eq + 1234*group[3], group[0], group[1], group[2])
                # p -= Poly(1234*group[3], *p.gens, domain = p.domain)
                p = Poly(eq, *group[0:3])
                
            except (PolynomialError, CoercionFailed, ZeroDivisionError), e:
                continue
            
            changed = False
            listterms = list(p.terms())
            if len(listterms) == 1:
                continue
            usedindices = set()

            equiv_zero_term = group[3]-group[0]**2-group[1]**2-group[2]**2
            
            for index0, index1 in combinations(range(len(listterms)),2):
                if index0 in usedindices or index1 in usedindices:
                    continue

                # In the following assignment,
                # the first  return value m contains powers of a power product
                # the second return value c is the coefficient
                m0, c0 = listterms[index0]
                m1, c1 = listterms[index1]
                
                if self.equal(c0, c1):
                    # replace x0**2+x1**2 by x3-x2**2
                    #         x1**2+x2**2 by x3-x0**2
                    #         x2**2+x0**2 by x3-x1**2
                    for i, j, k in permutations(range(3),3): #[(0,1,2), (0,2,1), (1,0,2), (1,2,0), (2,0,1), (2,1,0)]
                        if m0[k] == m1[k]:
                            
                            assert(m1[i] >= 0 and m0[j] >= 0)
                            if m0[i] == m1[i]+2 and m0[j]+2 == m1[j]:
                                #p += Poly(c0* \
                                #          equiv_zero_term* \
                                #          (group[k]**m0[k])*(group[i]**m1[i])*(group[j]**m0[j]), \
                                #          group[0], group[1], group[2])

                                q = eq + c0* \
                                          equiv_zero_term* \
                                          (group[k]**m0[k])*(group[i]**m1[i])*(group[j]**m0[j])

                                #assert(expand(p.as_expr()-q) == S.Zero)
                                changed = True
                            
                elif self.equal(c0, -c1):
                    # As x3 = x0**2 + x1**2 + x2**2
                    # x0**4 - x1**4 = (x0**2-x1**2)*(x0**2+x1**2) = (x0**2-x1**2)*(x3-x2**2)
                    for i, j, k in permutations(range(3),3): #[(0,1,2), (0,2,1), (1,0,2), (1,2,0), (2,0,1), (2,1,0)]
                        if m0[k] == m1[k]:
                            if m0[i] == 4 and m1[j] == 4:
                                #p += Poly(c0* \
                                #          group[k]**m0[k]* \
                                #          ((group[3]-group[k]**2)*(group[i]**2-group[j]**2) \
                                #           -group[i]**4 + group[j]**4), \
                                #          group[0], group[1], group[2])

                                q = eq + c0* \
                                          group[k]**m0[k]* \
                                          ((group[3]-group[k]**2)*(group[i]**2-group[j]**2) \
                                           -group[i]**4 + group[j]**4)

                                #assert(expand(p.as_expr()-q) == S.Zero)
                                changed = True
                                
                if changed:
                    neweq = q # p #.as_expr()
                    eq = neweq
                    usedindices.add(index0)
                    usedindices.add(index1)
                    break
                
        return neweq
    
    def _SimplifyRotationDot(self, eq, symbols, groups):
        """
        Simplify eq based on dot product being 0 
        or translation vector of the inverse of homogeneous matrix

        symbols is self._rotsymbols   or self._rotpossymbols
        groups  is self._rotdotgroups or self._rotposdotgroups

        Called by SimplifyTransform only.

        Recall

[[[0, 3], [1, 4], [2, 5], 0],
 [[0, 1], [3, 4], [6, 7], 0],
 [[0, 6], [1, 7], [2, 8], 0],
 [[0, 2], [3, 5], [6, 8], 0],
 [[3, 6], [4, 7], [5, 8], 0],
 [[1, 2], [4, 5], [7, 8], 0],
------------------------- Above are _rotdotgroups
------------------------- Below are _rotposdotgroups
 [[0, 9], [3, 10], [6, 11], npx],
 [[0, 12], [1, 13], [2, 14], px],
 [[1, 9], [4, 10], [7, 11], npy],
 [[3, 12], [4, 13], [5, 14], py],
 [[2, 9], [5, 10], [8, 11], npz],
 [[6, 12], [7, 13], [8, 14], pz]]

        """
        try:
            p = Poly(eq, *symbols)
        except (PolynomialError, CoercionFailed), e:
            return None
        
        changed = False
        listterms = list(p.terms())
        usedindices = set()

        rng_len_listterms = range(len(listterms))
        
        for g in groups:
            for i, j, k in [(0,1,2), (1,2,0), (2,0,1)]:

                gi0 = g[i][0]
                gi1 = g[i][1]
                gj0 = g[j][0]
                gj1 = g[j][1]

                for index0, index1 in combinations(rng_len_listterms, 2):

                    if index0 in usedindices or index1 in usedindices:
                        continue

                    m0, c0 = listterms[index0] 
                    m1, c1 = listterms[index1]
                    
                    if self.equal(c0, c1):
                        # TGN: sufficient condition of simplification may not be necessarily equal 
                        # 
                        # In the non-equal case, consider (a+b)*r00*r10 + (b+c)*r01*r11 + (c+a)*r02*r12 
                        # where a,b,c are distinct. 
                        # One of the acceptable results may be (c-a)*r01*r11 + (c-b)*r02*r12 
                        # INSTEAD of (-c)*r00*r10 + (-a)*r01*r11 + (-b)*r02*r12 
                        # 
                        # E.g. 5*r00*r10 + 3*r01*r11 + 4*r02*r12 = (-2)*r01*r11 + (-1)*r02*r12 
                        # 
                        # This observation only applies to DOT case, not to CROSS case
                        
                        if   m0[gi0] == 1 and m0[gi1] == 1 and m1[gj0] == 1 and m1[gj1] == 1: 
                            # make sure the left over terms are also the same 
                            m0l = list(m0); m0l[gi0] = 0; m0l[gi1] = 0 
                            m1l = list(m1); m1l[gj0] = 0; m1l[gj1] = 0
                            
                        elif m0[gj0] == 1 and m0[gj1] == 1 and m1[gi0] == 1 and m1[gi1] == 1: 
                            # make sure the left over terms are also the same 
                            m0l = list(m0); m0l[gj0] = 0; m0l[gj1] = 0 
                            m1l = list(m1); m1l[gi0] = 0; m1l[gi1] = 0

                        else:
                            continue
                                
                                 
                        if m0l == m1l: 
                            # m2l = list(m0l); m2l[gk0] += 1; m2l[gk1] += 1 
                            # deep copy list
                            gk0 = g[k][0]
                            gk1 = g[k][1]
                            m2l = m0l[:]; m2l[gk0] += 1; m2l[gk1] += 1 
                            m2 = tuple(m2l) 
                                
                            # there is a bug in sympy v0.6.7 polynomial adding here! 
                            # TGN: Now > 0.7, so no problem now?
                            
                            p = p.\
                                sub(Poly.from_dict({m0:c0}, *p.gens)). \
                                sub(Poly.from_dict({m1:c1}, *p.gens)). \
                                sub(Poly.from_dict({m2:c0}, *p.gens))

                            g3 = g[3] 
                            if g3 != S.Zero: 
                                # when g3 = npx, px, npy, py, npz, or pz 
                                new_m0 = tuple(m0l) 
                                p = p.add(Poly(g3, *p.gens)*Poly.from_dict({new_m0:c0}, *p.gens)) 
                            
                                        
                            changed = True
                            usedindices.add(index0)
                            usedindices.add(index1)
                            break

        return p if changed else None
    
    def _SimplifyRotationCross(self, eq, symbols, groups):
        """
        Simplify eq based on cross product being the remaining row/column

        symbols is self._rotsymbols     or self._rotpossymbols
        groups  is self._rotcrossgroups or self._rotposcrossgroups

        Called by SimplifyTransform only.
        """
        changed = False
        try:
            p = Poly(eq,*symbols)
        except (PolynomialError, CoercionFailed), e:
            return None

        listterms = list(p.terms())
        usedindices = set()
        rng_len_listterms = range(len(listterms))
        
        for cg in groups:

            cg00 = cg[0][0]
            cg01 = cg[0][1]
            cg10 = cg[1][0]
            cg11 = cg[1][1]
            
            for index0, index1 in combinations(rng_len_listterms, 2):
                
                if index0 in usedindices or index1 in usedindices:
                    continue

                m0, c0 = listterms[index0]
                m1, c1 = listterms[index1]
                
                if self.equal(c0, -c1):

                    if   m0[cg00] == 1 and m0[cg01] == 1 and m1[cg10] == 1 and m1[cg11] == 1:
                        # make sure the left over terms are also the same
                        m0l = list(m0); m0l[cg00] = 0; m0l[cg01] = 0
                        m1l = list(m1); m1l[cg10] = 0; m1l[cg11] = 0
                        
                    elif m0[cg10] == 1 and m0[cg11] == 1 and m1[cg00] == 1 and m1[cg01] == 1:
                        # make sure the left over terms are also the same
                        m0l = list(m0); m0l[cg10] = 0; m0l[cg11] = 0
                        m1l = list(m1); m1l[cg00] = 0; m1l[cg01] = 0
                        c0 = -c0
                        assert(self.equal(c0,c1))
                        
                    else:
                        continue

                    exec(ipython_str)
                        
                    if tuple(m0l) == tuple(m1l):
                        m2l = m0l[:]; m2l[cg[2]] += 1
                        m2 = tuple(m2l)
                        
                        # there is a bug in sympy polynomial caching here! (0.6.7)
                        # TGN: Now > 0.7, so no problem now?
                        p = p.\
                            sub(Poly.from_dict({m0:c0}, *p.gens)).\
                            add(Poly.from_dict({m1:c0}, *p.gens)).\
                            add(Poly.from_dict({m2:c0}, *p.gens))
                        changed = True
                        usedindices.add(index0)
                        usedindices.add(index1)
                        break
                        
        return p if changed else None

    def CheckExpressionUnique(self, exprs, expr, \
                              checknegative = True, \
                              removecommoncoeff = False):
        """
        Returns True is expr is NOT in exprs; False otherwise.

        If checknegative is True, then we also check if -expr is in exprs
        If removecommoncoeff is True, then we call self.removecommonexprs on expr first
        """
        if removecommoncoeff:
            expr = self.removecommonexprs(expr)
        
        for exprtest in exprs:

            if (\
                # (T,T)
                expr.is_Function and exprtest.is_Function \
                # RD: infinite loop for some reason if checking for this
                and (exprtest.func == sign \
                     or expr.func == sign) \
            ) \
                or \
                (\
                 # (T,T) or (F,F)
                 expr.is_Function == exprtest.is_Function \
                 and (self.equal(expr, exprtest) or (checknegative \
                                                    and self.equal(-expr, exprtest)))
                ):
                return False
            
        return True

    def getCommonExpression(self, exprs, expr):
        for i,exprtest in enumerate(exprs):
            if self.equal(expr,exprtest):
                return i
        return None

    def verifyAllEquations(self,AllEquations,unsolvedvars, solsubs, tree=None):
        extrazerochecks=[]
        for i in range(len(AllEquations)):
            expr = AllEquations[i]
            if not self.isValidSolution(expr):
                raise self.CannotSolveError('verifyAllEquations: equation is not valid: %s'%(str(expr)))
            
            if not expr.has(*unsolvedvars) and self.CheckExpressionUnique(extrazerochecks,expr):
                extrazerochecks.append(self.removecommonexprs(expr.subs(solsubs).evalf(),onlygcd=False,onlynumbers=True))
        if len(extrazerochecks) > 0:
            return [AST.SolverCheckZeros('verify',extrazerochecks,tree,[AST.SolverBreak('verifyAllEquations')],anycondition=False)]
        return tree

    def PropagateSolvedConstants(self, AllEquations, othersolvedvars, unknownvars, constantSymbols=None):
        """
        Sometimes equations can be like "npz", or "pp-1", which means npz=0 and pp=1. Check for these constraints and apply them to the rest of the equations
        Return a new set of equations
        :param constantSymbols: the variables to try to propagage, if None will use self.pvars
        """
        if constantSymbols is not None:
            constantSymbols = list(constantSymbols)
        else:
            constantSymbols = list(self.pvars)
        for othersolvedvar in othersolvedvars:
            constantSymbols.append(othersolvedvar)
            if self.IsHinge(othersolvedvar.name):
                constantSymbols.append(cos(othersolvedvar))
                constantSymbols.append(sin(othersolvedvar))
        newsubsdict = {}
        for eq in AllEquations:
            if not eq.has(*unknownvars) and eq.has(*constantSymbols):
                try:
                    reducedeq = self.SimplifyTransform(eq)
                    for constantSymbol in constantSymbols:
                        if eq.has(constantSymbol):
                            try:
                                peq = Poly(eq,constantSymbol)
                                if peq.degree(0) == 1:
                                    # equation is only degree 1 in the variable, and doesn't have any solvevars multiplied with it
                                    newsolution = solve(peq,constantSymbol)[0]
                                    if constantSymbol in newsubsdict:
                                        if self.codeComplexity(newsolution) < self.codeComplexity(newsubsdict[constantSymbol]):
                                            newsubsdict[constantSymbol] = newsolution
                                    else:
                                        newsubsdict[constantSymbol] = newsolution
                            except PolynomialError:
                                pass
                except PolynomialError, e:
                    # expected from simplifyTransform if eq is too complex
                    pass
                
        # first substitute everything that doesn't have othersolvedvar or unknownvars
        numberSubstitutions = []
        otherSubstitutions = []
        for var, value in newsubsdict.iteritems():
            if not value.has(*constantSymbols):
                numberSubstitutions.append((var,value))
            else:
                otherSubstitutions.append((var,value))
        NewEquations = []
        for ieq, eq in enumerate(AllEquations):
            if 1:#not eq.has(*unknownvars):
                neweq = eq.subs(numberSubstitutions).expand()
                if neweq != S.Zero:
                    # don't expand here since otherSubstitutions could make it very complicated
                    neweq2 = neweq.subs(otherSubstitutions)
                    if self.codeComplexity(neweq2) < self.codeComplexity(neweq)*2:
                        neweq2 = neweq2.expand()
                        if self.codeComplexity(neweq2) < self.codeComplexity(neweq) and neweq2 != S.Zero:
                            NewEquations.append(neweq2)
                        else:
                            NewEquations.append(neweq)
                    else:
                        NewEquations.append(neweq)
            else:
                NewEquations.append(eq)
        return NewEquations
    
    def SolveAllEquations(self, AllEquations, \
                          curvars, othersolvedvars, \
                          solsubs, endbranchtree, \
                          currentcases = None, \
                          unknownvars = [], \
                          currentcasesubs = None, \
                          canguessvars = True):
        """
        If canguessvars is True, then we can guess variable values, prodived they satisfy required conditions
        """
        from ikfast_AST import AST

        # range of progress is [0.15, 0.45].
        # Usually scopecounters can go to several hundred
        progress = 0.45 - 0.3/(1+self._scopecounter/100)
        self._CheckPreemptFn(progress = progress)
        
        if len(curvars) == 0:
            return endbranchtree
        
        self._scopecounter += 1
        scopecounter = int(self._scopecounter)
        log.info('depth = %d, c = %d\n' + \
                 '        %s, %s\n' + \
                 '        cases = %s', \
                 len(currentcases) if currentcases is not None else 0, \
                 self._scopecounter, othersolvedvars, curvars, \
                 None if currentcases is None or len(currentcases) is 0 else \
                 ("\n"+" "*16).join(str(x) for x in list(currentcases)))

        # solsubs = solsubs[:]

        # inverse substitutions
        # inv_freevarsubs = [(f[1],f[0]) for f in self.freevarsubs]
        # inv_solsubs     = [(f[1],f[0]) for f in solsubs         ]

        # single variable solutions
        solutions = []
        freevar_sol_subs = set().union(*[solsubs, self.freevarsubs])
        # equivalent to
        # self.freevarsubs + [solsub for solsub in solsubs if not solsub in self.freevarsubs]
        freevar = [f[0] for f in freevar_sol_subs]
        
        for curvar in curvars:
            othervars = unknownvars + [var for var in curvars if var != curvar]
            curvarsym = self.Variable(curvar)
            raweqns = []
            for eq in AllEquations:

                if (len(othervars) == 0 or \
                    not eq.has(*othervars)) and \
                    eq.has(curvar, curvarsym.htvar, curvarsym.cvar, curvarsym.svar):

                    if eq.has(*freevar):
                        # neweq = eq.subs(freevar_sol_subs)
                        # log.info('\n        %s\n\n-->     %s', eq, neweq)
                        # eq = neweq
                        eq = eq.subs(freevar_sol_subs)

                    if self.CheckExpressionUnique(raweqns, eq):
                        raweqns.append(eq)

            if len(raweqns) > 0:
                try:
                    rawsolutions = self.solveSingleVariable(\
                                                            self.sortComplexity(raweqns), \
                                                            curvar, othersolvedvars, \
                                                            unknownvars = curvars + unknownvars)

                    for solution in rawsolutions:
                        self.ComputeSolutionComplexity(solution, othersolvedvars, curvars)
                        if solution.numsolutions() > 0:
                            solutions.append((solution, curvar))
                        else:
                            log.warn('solution did not have any equations')

                except self.CannotSolveError:
                    pass

        # Only return here if a solution was found that perfectly determines the unknown
        # Otherwise, the pairwise solver could come up with something.
        #
        # There is still a problem with this: (bertold robot)
        # Sometimes an equation like atan2(y,x) evaluates to atan2(0,0) during runtime.
        # This cannot be known at compile time, so the equation is selected and any other possibilities are rejected.
        # In the bertold robot case, the next possibility is a pair-wise solution involving two variables
        #
        # TGN: don't we check Abs(y)+Abs(x) for atan2?

        # exec(ipython_str) in globals(), locals()
        
        if any([s[0].numsolutions() == 1 for s in solutions]):
            return self.AddSolution(solutions, \
                                    AllEquations, \
                                    curvars, \
                                    othersolvedvars, \
                                    solsubs, \
                                    endbranchtree, \
                                    currentcases = currentcases, \
                                    currentcasesubs = currentcasesubs, \
                                    unknownvars = unknownvars)
        
        curvarsubssol = []
        for var0, var1 in combinations(curvars,2):
            othervars = unknownvars + \
                        [var for var in curvars if var != var0 and var != var1]
            raweqns = []
            complexity = 0
            for eq in AllEquations:
                if (len(othervars) == 0 or not eq.has(*othervars)) \
                   and eq.has(var0, var1):
                    
                    eq = eq.subs(self.freevarsubs + solsubs)
                    if self.CheckExpressionUnique(raweqns, eq):
                        raweqns.append(eq)
                        complexity += self.codeComplexity(eq)
                        
            if len(raweqns) > 1:
                curvarsubssol.append((var0, var1, raweqns, complexity))
                
        curvarsubssol.sort(lambda x, y: x[3]-y[3])
        
        if len(curvars) == 2 and \
           self.IsHinge(curvars[0].name) and \
           self.IsHinge(curvars[1].name) and \
           len(curvarsubssol) > 0:
            # There are only two variables left, so two possibilities:
            #
            # EITHER two axes are aligning, OR these two variables depend on each other.
            #
            # Note that the axes' anchors also have to be along the direction!
            
            var0, var1, raweqns, complexity = curvarsubssol[0]
            dummyvar = Symbol('dummy')
            dummyvalue = var0 + var1
            NewEquations = []
            NewEquationsAll = []
            hasExtraConstraints = False
            for eq in raweqns:

                # TGN: ensure curvars is a subset of self.trigvars_subs
                assert(len([z for z in curvars if z in self.trigvars_subs]) == len(curvars))
                # equivalent?
                assert(not any([(z not in self.trigvars_subs) for z in curvars]))

                # try dummyvar = var0 + var1
                neweq = self.trigsimp_new(eq.subs(var0, dummyvar-var1).expand(trig=True))
                eq = neweq.subs(self.freevarsubs+solsubs)
                if self.CheckExpressionUnique(NewEquationsAll, eq):
                    NewEquationsAll.append(eq)
                    
                if neweq.has(dummyvar):
                    if neweq.has(*(othervars+curvars)):
                        hasExtraConstraints = True
                        # break
                        # don't know why breaking here ...
                        # sometimes equations can be very complex but variables can still be dependent
                    else:
                        eq = neweq.subs(self.freevarsubs + solsubs)
                        if self.CheckExpressionUnique(NewEquations, eq):
                            NewEquations.append(eq)
                            
            if len(NewEquations) < 2 and hasExtraConstraints:
                # try dummyvar = var0 - var1
                NewEquations = []
                NewEquationsAll = []
                hasExtraConstraints = False
                dummyvalue = var0 - var1
                
                for eq in raweqns:
                    # TGN: ensure curvars is a subset of self.trigvars_subs
                    assert(len([z for z in curvars if z in self.trigvars_subs]) == len(curvars))
                    # equivalent?
                    assert(not any([(z not in self.trigvars_subs) for z in curvars]))
                        
                    neweq = self.trigsimp_new(eq.subs(var0, dummyvar + var1).expand(trig = True))
                    eq = neweq.subs(self.freevarsubs + solsubs)
                    
                    if self.CheckExpressionUnique(NewEquationsAll, eq):
                        NewEquationsAll.append(eq)
                        
                    if neweq.has(dummyvar):
                        if neweq.has(*(othervars + curvars)):
                            hasExtraConstraints = True
                            # break
                            # don't know why breaking here ...
                            # sometimes equations can be too complex but variables can still be dependent
                        else:
                            eq = neweq.subs(self.freevarsubs + solsubs)
                            if self.CheckExpressionUnique(NewEquations, eq):
                                NewEquations.append(eq)
                                
            if len(NewEquations) >= 2:
                dummysolutions = []
                try:
                    rawsolutions = self.solveSingleVariable(NewEquations, dummyvar, othersolvedvars, \
                                                            unknownvars = curvars+unknownvars)
                    for solution in rawsolutions:
                        self.ComputeSolutionComplexity(solution, othersolvedvars, curvars)
                        dummysolutions.append(solution)
                        
                except self.CannotSolveError:
                    pass
                
                if any([s.numsolutions()==1 for s in dummysolutions]):
                    # two axes are aligning, so modify the solutions to reflect the original variables and add a free variable
                    log.info('found two aligning axes %s: %r', dummyvalue, NewEquations)
                    solutions = []
                    for dummysolution in dummysolutions:
                        if dummysolution.numsolutions() != 1:
                            continue
                        if dummysolution.jointevalsin is not None or \
                           dummysolution.jointevalcos is not None:
                            log.warn('dummy solution should not have sin/cos parts!')

                        sindummyvarsols = []
                        cosdummyvarsols = []
                        for eq in NewEquations:
                            sols = solve(eq, sin(dummyvar))
                            sindummyvarsols += sols
                            sols = solve(eq, cos(dummyvar))
                            cosdummyvarsols += sols
                        
                        # double check with NewEquationsAll that everything evaluates to 0
                        newsubs = [( value,  sin(dummyvar)) for value in sindummyvarsols] + \
                                  [( value,  cos(dummyvar)) for value in cosdummyvarsols] + \
                                  [(-value, -sin(dummyvar)) for value in sindummyvarsols] + \
                                  [(-value, -cos(dummyvar)) for value in cosdummyvarsols]
                        allzeros = True
                        for eq in NewEquationsAll:
                            if trigsimp(eq.subs(newsubs)) != S.Zero:
                                allzeros = False
                                break
                            
                        if allzeros:
                            solution = AST.SolverSolution(curvars[0].name, \
                                                          isHinge = self.IsHinge(curvars[0].name))
                            solution.jointeval = [dummysolution.jointeval[0] - dummyvalue + curvars[0]]
                            self.ComputeSolutionComplexity(solution, othersolvedvars, curvars)
                            solutions.append((solution, curvars[0]))
                        else:
                            log.warn('not all equations evaluate to zero, so %s vars are not collinear', curvars)
                            
                    if len(solutions) > 0:
                        tree = self.AddSolution(solutions, raweqns, curvars[0:1], \
                                                othersolvedvars + curvars[1:2], \
                                                solsubs + self.Variable(curvars[1]).subs, \
                                                endbranchtree, \
                                                currentcases = currentcases, \
                                                currentcasesubs = currentcasesubs,
                                                unknownvars = unknownvars)
                        if tree is not None:
                            return [AST.SolverFreeParameter(curvars[1].name, tree)]
                else:
                    log.warn('almost found two axes but num solutions was: %r', \
                             [s.numsolutions() == 1 for s in dummysolutions])
                    
        for var0, var1, raweqns, complexity in curvarsubssol:
            try:
                rawsolutions = self.SolvePrismaticHingePairVariables(raweqns, var0, var1, \
                                                                     othersolvedvars, \
                                                                     unknownvars = curvars + unknownvars)
                for solution in rawsolutions:
                    # solution.subs(inv_freevarsubs)
                    self.ComputeSolutionComplexity(solution, othersolvedvars, curvars)
                    solutions.append((solution, Symbol(solution.jointname)))
                    
                if len(rawsolutions) > 0: # solving a pair is rare, so any solution will do
                    # TGN: so we don't try others in the for-loop?
                    break
            except self.CannotSolveError:
                pass
            
        for var0, var1, raweqns, complexity in curvarsubssol:
            try:
                rawsolutions = self.SolvePairVariables(raweqns, var0, var1, \
                                                       othersolvedvars, \
                                                       unknownvars = curvars + unknownvars)
            except self.CannotSolveError, e:
                log.debug(e)
#                 try:
#                     rawsolutions=self.SolvePrismaticHingePairVariables(raweqns,var0,var1,othersolvedvars,unknownvars=curvars+unknownvars)
#                 except self.CannotSolveError, e:
#                     log.debug(e)
                rawsolutions = []
            for solution in rawsolutions:
                #solution.subs(inv_freevarsubs)
                try:
                    self.ComputeSolutionComplexity(solution, othersolvedvars, curvars)
                    solutions.append((solution, Symbol(solution.jointname)))
                except self.CannotSolveError, e:
                    log.warn(u'equation failed to compute solution complexity: %s', solution.jointeval)
            if len(rawsolutions) > 0: # solving a pair is rare, so any solution will do
                # TGN: so we don't try others in the for-loop?
                break
                        
        # take the least complex solution and go on
        if len(solutions) > 0:
            return self.AddSolution(solutions, AllEquations, \
                                    curvars, othersolvedvars, \
                                    solsubs, \
                                    endbranchtree, \
                                    currentcases = currentcases, \
                                    currentcasesubs = currentcasesubs, \
                                    unknownvars = unknownvars)
        
        # test with higher degrees, necessary?
        for curvar in curvars:
            othervars = unknownvars + [var for var in curvars if var != curvar]
            raweqns = []
            for eq in AllEquations:
                if (len(othervars) == 0 or not eq.has(*othervars)) and eq.has(curvar):
                    eq = eq.subs(self.freevarsubs + solsubs)
                    if self.CheckExpressionUnique(raweqns, eq):
                        raweqns.append(eq)
                        
            for raweqn in raweqns:
                try:
                    log.debug('testing with higher degrees')
                    solution = self.solveHighDegreeEquationsHalfAngle([raweqn], self.Variable(curvar))
                    self.ComputeSolutionComplexity(solution, othersolvedvars, curvars)
                    solutions.append((solution, curvar))
                    
                except self.CannotSolveError:
                    pass
               
        if len(solutions) > 0:
            return self.AddSolution(solutions, AllEquations, \
                                    curvars, othersolvedvars, \
                                    solsubs, \
                                    endbranchtree, \
                                    currentcases = currentcases, \
                                    currentcasesubs = currentcasesubs, \
                                    unknownvars = unknownvars)
        
        # solve with all 3 variables together?
#         htvars = [self.Variable(varsym).htvar for varsym in curvars]
#         reducedeqs = []
#         for eq in AllEquations:
#             if eq.has(*curvars):
#                 num, denom, htvarsubsinv = self.ConvertSinCosEquationToHalfTan(eq, curvars)
#                 reducedeqs.append(Poly(num, *htvars))
# 

        # only guess if final joint to be solved, or there exists current cases and at least one joint has been solved already.
        # don't want to start guessing when no joints have been solved yet, this indicates bad equations
        if canguessvars and \
           len(othersolvedvars) + len(curvars) == len(self.freejointvars) + len(self._solvejointvars) and \
           (len(curvars) == 1 or (len(curvars) < len(self._solvejointvars) and \
                                  currentcases is not None and \
                                  len(currentcases) > 0)): # only estimate when deep in the hierarchy, do not want the guess to be executed all the time
            # perhaps there's a degree of freedom that is not trivial to compute?
            # take the highest hinge variable and set it
            log.info('trying to guess variable from %r', curvars)
            return self.GuessValuesAndSolveEquations(AllEquations, \
                                                     curvars, othersolvedvars, solsubs, \
                                                     endbranchtree, \
                                                     currentcases, \
                                                     unknownvars, \
                                                     currentcasesubs)
        
        # have got this far, so perhaps two axes are aligned?
        #
        # TGN: so we cannot detect such aligning before?
        #
        raise self.CannotSolveError('SolveAllEquations failed to find a variable to solve')
    
    def _SubstituteGlobalSymbols(self, eq, globalsymbols = None):
        if globalsymbols is None:
            globalsymbols = self.globalsymbols
        preveq = eq
        neweq = preveq.subs(globalsymbols)
        while preveq != neweq:
            if not self.isValidSolution(neweq):
                raise self.CannotSolveError('equation %r is not valid'%neweq)
            
            preveq = neweq
            neweq = preveq.subs(globalsymbols)    
        return neweq
    
    def _AddToGlobalSymbols(self, var, eq):
        """adds to the global symbols, returns True if replaced with an existing entry
        """
        for iglobal, gvarexpr in enumerate(self.globalsymbols):
            if var == gvarexpr[0]:
                self.globalsymbols[iglobal] = (var, eq)
                return True
            
        self.globalsymbols.append((var, eq))
        return False
        
    def AddSolution(self, solutions, AllEquations, \
                    curvars, othersolvedvars, \
                    solsubs, endbranchtree, \
                    currentcases = None, \
                    currentcasesubs = None, \
                    unknownvars = None):
        """
        Take the least complex solution of a set of solutions and resume solving
        """
        from ikfast_AST import AST
        
        self._CheckPreemptFn()
        self._scopecounter += 1
        scopecounter = int(self._scopecounter)
        solutions = [s for s in solutions if s[0].score < oo and s[0].checkValidSolution()] # remove infinite scores
        if len(solutions) == 0:
            raise self.CannotSolveError('no valid solutions')
        
        if unknownvars is None:
            unknownvars = []
        solutions.sort(lambda x, y: x[0].score-y[0].score)
        hasonesolution = False
        for solution in solutions:
            checkforzeros = solution[0].checkforzeros
            hasonesolution |= solution[0].numsolutions() == 1
            if len(checkforzeros) == 0 and solution[0].numsolutions() == 1:
                # did find a good solution, so take it. Make sure to check any zero branches
                var = solution[1]
                newvars=curvars[:]
                newvars.remove(var)
                return [solution[0].subs(solsubs)]+self.SolveAllEquations(AllEquations,curvars=newvars,othersolvedvars=othersolvedvars+[var],solsubs=solsubs+self.Variable(var).subs,endbranchtree=endbranchtree, currentcases=currentcases, currentcasesubs=currentcasesubs, unknownvars=unknownvars)
            
        if not hasonesolution:
            # check again except without the number of solutions requirement
            for solution in solutions:
                checkforzeros = solution[0].checkforzeros
                if len(checkforzeros) == 0:
                    # did find a good solution, so take it. Make sure to check any zero branches
                    var = solution[1]
                    newvars=curvars[:]
                    newvars.remove(var)
                    return [solution[0].subs(solsubs)]+self.SolveAllEquations(AllEquations,curvars=newvars,othersolvedvars=othersolvedvars+[var],solsubs=solsubs+self.Variable(var).subs,endbranchtree=endbranchtree,currentcases=currentcases, currentcasesubs=currentcasesubs, unknownvars=unknownvars)

        originalGlobalSymbols = self.globalsymbols        
        # all solutions have check for zero equations
        # choose the variable with the shortest solution and compute (this is a conservative approach)
        usedsolutions = []
        # remove any solutions with similar checkforzero constraints (because they are essentially the same)
        for solution,var in solutions:
            solution.subs(solsubs)
            if len(usedsolutions) == 0:
                usedsolutions.append((solution,var))
            else:
                match = False
                for usedsolution,usedvar in usedsolutions:
                    if len(solution.checkforzeros) == len(usedsolution.checkforzeros):
                        if not any([self.CheckExpressionUnique(usedsolution.checkforzeros,eq) for eq in solution.checkforzeros]):
                            match = True
                            break
                if not match:
                    usedsolutions.append((solution,var))
                    if len(usedsolutions) >= 3:
                        # don't need more than three alternatives (used to be two, but then lookat barrettwam4 proved that wrong)
                        break

        nextsolutions = dict()
        allvars = []
        for v in curvars:
            allvars += self.Variable(v).vars
        allothersolvedvars = []
        for v in othersolvedvars:
            allothersolvedvars += self.Variable(v).vars
        lastbranch = []
        prevbranch=lastbranch
        if currentcases is None:
            currentcases = set()
        if currentcasesubs is None:
            currentcasesubs = list()
        if self.degeneratecases is None:
            self.degeneratecases = self.DegenerateCases()
        handledconds = self.degeneratecases.GetHandledConditions(currentcases)
        # one to one correspondence with usedsolutions and the SolverCheckZeros hierarchies (used for cross product of equations later on)
        zerosubstitutioneqs = [] # indexed by reverse ordering of usedsolutions (len(usedsolutions)-solutionindex-1)
        # zerosubstitutioneqs equations flattened for easier checking
        flatzerosubstitutioneqs = []
        hascheckzeros = False
        
        addhandleddegeneratecases = [] # for bookkeeping/debugging
        
        # iterate in reverse order and put the most recently processed solution at the front.
        # There is a problem with this algorithm transferring the degenerate cases correctly.
        # Although the zeros of the first equation are checked, they are not added as conditions
        # to the later equations, so that the later equations will also use variables as unknowns (even though they are determined to be specific constants). This is most apparent in rotations.
        for solution,var in usedsolutions[::-1]:
            # there are divide by zeros, so check if they can be explicitly solved for joint variables
            checkforzeros = []
            localsubstitutioneqs = []
            for checkzero in solution.checkforzeros:
                if checkzero.has(*allvars):
                    log.info('ignoring special check for zero since it has symbols %s: %s',str(allvars),str(checkzero))
                    continue
                
                # bother trying to extract something if too complex (takes a lot of computation time to check and most likely nothing will be extracted). 100 is an arbitrary value
                checkzeroComplexity = self.codeComplexity(checkzero)
                if checkzeroComplexity > 120: 
                    log.warn('checkforzero too big (%d): %s', checkzeroComplexity, checkzero)
                    # don't even add it if it is too big
                    if checkzeroComplexity < 500:
                        checkforzeros.append(checkzero)#self.removecommonexprs(checkzero.evalf(),onlygcd=False,onlynumbers=True))
                else:
                    checkzero2 = self._SubstituteGlobalSymbols(checkzero, originalGlobalSymbols)
                    checkzero2Complexity = self.codeComplexity(checkzero2)
                    if checkzero2Complexity < 2*checkzeroComplexity: # check that with substitutions, things don't get too big
                        checkzero = checkzero2
                        # fractions could get big, so evaluate directly
                        checkzeroeval = checkzero.evalf()
                        if checkzero2Complexity < self.codeComplexity(checkzeroeval):
                            checkforzeros.append(checkzero)
                        else:
                            checkforzeros.append(checkzero.evalf())#self.removecommonexprs(checkzero.evalf(),onlygcd=False,onlynumbers=True)
                    checksimplezeroexprs = [checkzero]
                    if not checkzero.has(*allothersolvedvars):
                        sumsquaresexprs = self._GetSumSquares(checkzero)
                        if sumsquaresexprs is not None:
                            checksimplezeroexprs += sumsquaresexprs
                            sumsquaresexprstozero = []
                            for sumsquaresexpr in sumsquaresexprs:
                                if sumsquaresexpr.is_Symbol:
                                    sumsquaresexprstozero.append(sumsquaresexpr)
                                elif sumsquaresexpr.is_Mul:
                                    for arg in sumsquaresexpr.args:
                                        if arg.is_Symbol:
                                            sumsquaresexprstozero.append(arg)
                            if len(sumsquaresexprstozero) > 0:
                                localsubstitutioneqs.append([sumsquaresexprstozero,checkzero,[(sumsquaresexpr,S.Zero) for sumsquaresexpr in sumsquaresexprstozero], []])
                                handledconds += sumsquaresexprstozero
                    for checksimplezeroexpr in checksimplezeroexprs:
                        #if checksimplezeroexpr.has(*othersolvedvars): # cannot do this check since sjX,cjX might be used
                        for othervar in othersolvedvars:
                            sothervar = self.Variable(othervar).svar
                            cothervar = self.Variable(othervar).cvar
                            if checksimplezeroexpr.has(othervar,sothervar,cothervar):
                                # the easiest thing to check first is if the equation evaluates to zero on boundaries 0,pi/2,pi,-pi/2
                                s = AST.SolverSolution(othervar.name,jointeval=[],isHinge=self.IsHinge(othervar.name))
                                for value in [S.Zero,pi/2,pi,-pi/2]:
                                    try:
                                        # doing (1/x).subs(x,0) produces a RuntimeError (infinite recursion...)
                                        checkzerosub=checksimplezeroexpr.subs([(othervar,value),(sothervar,sin(value).evalf(n=30)),(cothervar,cos(value).evalf(n=30))])
                                        
                                        if self.isValidSolution(checkzerosub) and checkzerosub.evalf(n=30) == S.Zero:
                                            if s.jointeval is None:
                                                s.jointeval = []
                                            s.jointeval.append(S.One*value)
                                    except (RuntimeError, AssertionError),e: # 
                                        log.warn('othervar %s=%f: %s',str(othervar),value,e)

                                if s.jointeval is not None and len(s.jointeval) > 0:
                                    ss = [s]
                                else:
                                    ss = []
                                try:
                                    # checksimplezeroexpr can be simple like -cj4*r21 - r20*sj4
                                    # in which case the solutions would be [-atan2(-r21, -r20), -atan2(-r21, -r20) + 3.14159265358979]
                                    ss += self.solveSingleVariable([checksimplezeroexpr.subs([(sothervar,sin(othervar)),(cothervar,cos(othervar))])],othervar,othersolvedvars)
                                except PolynomialError:
                                    # checksimplezeroexpr was too complex
                                    pass
                                
                                except self.CannotSolveError,e:
                                    # this is actually a little tricky, sometimes really good solutions can have a divide that looks like:
                                    # ((0.405 + 0.331*cj2)**2 + 0.109561*sj2**2 (manusarm_left)
                                    # This will never be 0, but the solution cannot be solved. Instead of rejecting, add a condition to check if checksimplezeroexpr itself is 0 or not
                                    pass
                                
                                for s in ss:
                                    # can actually simplify Positions and possibly get a new solution!
                                    if s.jointeval is not None:
                                        for eq in s.jointeval:
                                            eq = self._SubstituteGlobalSymbols(eq, originalGlobalSymbols)
                                            # why checking for just number? ok to check if solution doesn't contain any other variableS?
                                            # if the equation is non-numerical, make sure it isn't deep in the degenerate cases
                                            if eq.is_number or (len(currentcases) <= 1 and not eq.has(*allothersolvedvars) and self.codeComplexity(eq) < 100):
                                                isimaginary = self.AreAllImaginaryByEval(eq) or  eq.evalf().has(I)
                                                # TODO should use the fact that eq is imaginary
                                                if isimaginary:
                                                    log.warn('eq %s is imaginary, but currently do not support this', eq)
                                                    continue
                                                
                                                dictequations = []
                                                if not eq.is_number and not eq.has(*allothersolvedvars):
                                                    # not dependent on variables, so it could be in the form of atan(px,py), so convert to a global symbol since it never changes
                                                    sym = self.gsymbolgen.next()
                                                    dictequations.append((sym,eq))
                                                    #eq = sym
                                                    sineq = self.gsymbolgen.next()
                                                    dictequations.append((sineq,self.SimplifyAtan2(sin(eq))))
                                                    coseq = self.gsymbolgen.next()
                                                    dictequations.append((coseq,self.SimplifyAtan2(cos(eq))))
                                                else:
                                                    sineq = sin(eq).evalf(n=30)
                                                    coseq = cos(eq).evalf(n=30)
                                                cond=Abs(othervar-eq.evalf(n=30))
                                                if self.CheckExpressionUnique(handledconds, cond):
                                                    if self.IsHinge(othervar.name):
                                                        evalcond=fmod(cond+pi,2*pi)-pi
                                                    else:
                                                        evalcond=cond
                                                    localsubstitutioneqs.append([[cond],evalcond,[(sothervar,sineq),(sin(othervar),sineq),(cothervar,coseq),(cos(othervar),coseq),(othervar,eq)], dictequations])
                                                    handledconds.append(cond)
                                    elif s.jointevalsin is not None:
                                        for eq in s.jointevalsin:
                                            eq = self.SimplifyAtan2(self._SubstituteGlobalSymbols(eq, originalGlobalSymbols))
                                            if eq.is_number or (len(currentcases) <= 1 and not eq.has(*allothersolvedvars) and self.codeComplexity(eq) < 100):
                                                dictequations = []
                                                # test when cos(othervar) > 0
                                                # don't use asin(eq)!! since eq = (-pz**2/py**2)**(1/2), which would produce imaginary numbers
                                                #cond=othervar-asin(eq).evalf(n=30)
                                                # test if eq is imaginary, if yes, then only solution is when sothervar==0 and eq==0
                                                isimaginary = self.AreAllImaginaryByEval(eq) or  eq.evalf().has(I)
                                                if isimaginary:
                                                    cond = abs(sothervar) + abs((eq**2).evalf(n=30)) + abs(sign(cothervar)-1)
                                                else:
                                                    if not eq.is_number and not eq.has(*allothersolvedvars):
                                                        # not dependent on variables, so it could be in the form of atan(px,py), so convert to a global symbol since it never changes
                                                        sym = self.gsymbolgen.next()
                                                        dictequations.append((sym,eq))
                                                        #eq = sym
                                                    cond=abs(sothervar-eq.evalf(n=30)) + abs(sign(cothervar)-1)
                                                if self.CheckExpressionUnique(handledconds, cond):
                                                    if self.IsHinge(othervar.name):
                                                        evalcond=fmod(cond+pi,2*pi)-pi
                                                    else:
                                                        evalcond=cond
                                                    if isimaginary:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,S.Zero),(sin(othervar),S.Zero),(cothervar,S.One),(cos(othervar),S.One),(othervar,S.One)], dictequations])
                                                    else:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,eq),(sin(othervar),eq),(cothervar,sqrt(1-eq*eq).evalf(n=30)),(cos(othervar),sqrt(1-eq*eq).evalf(n=30)),(othervar,asin(eq).evalf(n=30))], dictequations])
                                                    handledconds.append(cond)
                                                # test when cos(othervar) < 0
                                                if isimaginary:
                                                    cond = abs(sothervar) + abs((eq**2).evalf(n=30)) + abs(sign(cothervar)+1)
                                                else:
                                                    cond=abs(sothervar-eq.evalf(n=30))+abs(sign(cothervar)+1)
                                                #cond=othervar-(pi-asin(eq).evalf(n=30))
                                                if self.CheckExpressionUnique(handledconds, cond):
                                                    if self.IsHinge(othervar.name):
                                                        evalcond=fmod(cond+pi,2*pi)-pi
                                                    else:
                                                        evalcond=cond
                                                    if isimaginary:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,S.Zero),(sin(othervar),S.Zero),(cothervar,-S.One),(cos(othervar),-S.One),(othervar,pi.evalf(n=30))], dictequations])
                                                    else:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,eq),(sin(othervar),eq),(cothervar,-sqrt(1-eq*eq).evalf(n=30)),(cos(othervar),-sqrt(1-eq*eq).evalf(n=30)),(othervar,(pi-asin(eq)).evalf(n=30))], dictequations])
                                                    handledconds.append(cond)
                                    elif s.jointevalcos is not None:
                                        for eq in s.jointevalcos:
                                            eq = self.SimplifyAtan2(self._SubstituteGlobalSymbols(eq, originalGlobalSymbols))
                                            if eq.is_number or (len(currentcases) <= 1 and not eq.has(*allothersolvedvars) and self.codeComplexity(eq) < 100):
                                                dictequations = []
                                                # test when sin(othervar) > 0
                                                # don't use acos(eq)!! since eq = (-pz**2/px**2)**(1/2), which would produce imaginary numbers
                                                # that's why check eq.evalf().has(I)
                                                #cond=othervar-acos(eq).evalf(n=30)
                                                isimaginary = self.AreAllImaginaryByEval(eq) or  eq.evalf().has(I)
                                                if isimaginary:
                                                    cond=abs(cothervar)+abs((eq**2).evalf(n=30)) + abs(sign(sothervar)-1)
                                                else:
                                                    if not eq.is_number and not eq.has(*allothersolvedvars):
                                                        # not dependent on variables, so it could be in the form of atan(px,py), so convert to a global symbol since it never changes
                                                        sym = self.gsymbolgen.next()
                                                        dictequations.append((sym,eq))
                                                        eq = sym
                                                    cond=abs(cothervar-eq.evalf(n=30)) + abs(sign(sothervar)-1)
                                                if self.CheckExpressionUnique(handledconds, cond):
                                                    if self.IsHinge(othervar.name):
                                                        evalcond=fmod(cond+pi,2*pi)-pi
                                                    else:
                                                        evalcond=cond
                                                    if isimaginary:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,S.One),(sin(othervar),S.One),(cothervar,S.Zero),(cos(othervar),S.Zero),(othervar,(pi/2).evalf(n=30))], dictequations])
                                                    else:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,sqrt(1-eq*eq).evalf(n=30)),(sin(othervar),sqrt(1-eq*eq).evalf(n=30)),(cothervar,eq),(cos(othervar),eq),(othervar,acos(eq).evalf(n=30))], dictequations])
                                                    handledconds.append(cond)
                                                #cond=othervar+acos(eq).evalf(n=30)
                                                if isimaginary:
                                                    cond=abs(cothervar)+abs((eq**2).evalf(n=30)) + abs(sign(sothervar)+1)
                                                else:
                                                    cond=abs(cothervar-eq.evalf(n=30)) + abs(sign(sothervar)+1)
                                                if self.CheckExpressionUnique(handledconds, cond):
                                                    if self.IsHinge(othervar.name):
                                                        evalcond=fmod(cond+pi,2*pi)-pi
                                                    else:
                                                        evalcond=cond
                                                    if isimaginary:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,-S.One),(sin(othervar),-S.One),(cothervar,S.Zero),(cos(othervar),S.Zero),(othervar,(-pi/2).evalf(n=30))], dictequations])
                                                    else:
                                                        localsubstitutioneqs.append([[cond],evalcond,[(sothervar,-sqrt(1-eq*eq).evalf(n=30)),(sin(othervar),-sqrt(1-eq*eq).evalf(n=30)),(cothervar,eq),(cos(othervar),eq),(othervar,-acos(eq).evalf(n=30))], dictequations])
                                                    handledconds.append(cond)
            flatzerosubstitutioneqs += localsubstitutioneqs
            zerosubstitutioneqs.append(localsubstitutioneqs)
            if not var in nextsolutions:
                try:
                    newvars=curvars[:]
                    newvars.remove(var)
                    # degenreate cases should get restored here since once we go down a particular branch, there's no turning back
                    olddegeneratecases = self.degeneratecases
                    self.degeneratecases = olddegeneratecases.Clone()
                    nextsolutions[var] = self.SolveAllEquations(AllEquations,curvars=newvars,othersolvedvars=othersolvedvars+[var],solsubs=solsubs+self.Variable(var).subs,endbranchtree=endbranchtree,currentcases=currentcases, currentcasesubs=currentcasesubs, unknownvars=unknownvars)
                finally:
                    addhandleddegeneratecases += olddegeneratecases.handleddegeneratecases
                    self.degeneratecases = olddegeneratecases
            if len(checkforzeros) > 0:
                hascheckzeros = True                
                solvercheckzeros = AST.SolverCheckZeros(jointname=var.name,jointcheckeqs=checkforzeros,nonzerobranch=[solution]+nextsolutions[var],zerobranch=prevbranch,anycondition=True,thresh=solution.GetZeroThreshold())
                # have to transfer the dictionary!
                solvercheckzeros.dictequations = originalGlobalSymbols + solution.dictequations                    
                solvercheckzeros.equationsused = AllEquations
                solution.dictequations = []
                prevbranch=[solvercheckzeros]
            else:
                prevbranch = [solution]+nextsolutions[var]
        
        if len(prevbranch) == 0:
            raise self.CannotSolveError('failed to add solution!')

        maxlevel2scopecounter = 300 # used to limit how deep the hierarchy goes or otherwise IK can get too big
        if len(currentcases) >= self.maxcasedepth or (scopecounter > maxlevel2scopecounter and len(currentcases) >= 2):
            log.warn('c = %d, %d levels deep in checking degenerate cases, skip\n' + \
                     '        curvars = %r\n' + \
                     '        AllEquations = %s', \
                     scopecounter, len(currentcases), curvars, \
                     ("\n"+" "*23).join(str(x) for x in list(AllEquations)))
            
            lastbranch.append(AST.SolverBreak('%d cases reached'%self.maxcasedepth, [(var,self.SimplifyAtan2(self._SubstituteGlobalSymbols(eq, originalGlobalSymbols))) for var, eq in currentcasesubs], othersolvedvars, solsubs, originalGlobalSymbols, endbranchtree))
            return prevbranch
        
        # fill the last branch with all the zero conditions
        if hascheckzeros:
            # count the number of rotation symbols seen in the current cases
            numRotSymbolsInCases = 0
            if self._iktype == 'transform6d' or self._iktype == 'rotation3d':
                rotsymbols = set(self.Tee[:3,:3]).union([Symbol('new_r00'), Symbol('new_r01'), Symbol('new_r02'), Symbol('new_r10'), Symbol('new_r11'), Symbol('new_r12'), Symbol('new_r20'), Symbol('new_r21'), Symbol('new_r22')])
                for var, eq in currentcasesubs:
                    if var in rotsymbols:
                        numRotSymbolsInCases += 1
            else:
                rotsymbols = []
            # if not equations found, try setting two variables at once
            # also try setting px, py, or pz to 0 (barrettwam4 lookat)
            # sometimes can get the following: cj3**2*sj4**2 + cj4**2
            threshnumsolutions = 1 # # number of solutions to take usedsolutions[:threshnumsolutions] for the dual values
            for isolution,(solution,var) in enumerate(usedsolutions[::-1]):
                if isolution < len(usedsolutions)-threshnumsolutions and len(flatzerosubstitutioneqs) > 0:
                    # have at least one zero condition...
                    continue
                localsubstitutioneqs = []
                for checkzero in solution.checkforzeros:
                    if checkzero.has(*allvars):
                        log.info('ignoring special check for zero 2 since it has symbols %s: %s',str(allvars), str(checkzero))
                        continue
                    
                    # don't bother trying to extract something if too complex (takes a lot of computation time to check and most likely nothing will be extracted). 120 is an arbitrary value
                    if self.codeComplexity(checkzero) > 120:
                        continue
                    
                    possiblesubs = []
                    ishinge = []
                    for preal in self.Tee[:3,3]:
                        if checkzero.has(preal):
                            possiblesubs.append([(preal,S.Zero)])
                            ishinge.append(False)
                    # have to be very careful with the rotations since they are dependent on each other. For example if r00 and r01 are both 0, then r02 is +- 1, and r12 and r22 are 0. Then r10, r12, r20, r21 is a 2D rotation matrix
                    if numRotSymbolsInCases < 2:
                        for preal in rotsymbols:
                            if checkzero.has(preal):
                                possiblesubs.append([(preal,S.Zero)])
                                ishinge.append(False)
                    for othervar in othersolvedvars:
                        othervarobj = self.Variable(othervar)
                        if checkzero.has(*othervarobj.vars):
                            if not self.IsHinge(othervar.name):
                                possiblesubs.append([(othervar,S.Zero)])
                                ishinge.append(False)
                                continue
                            else:
                                sothervar = othervarobj.svar
                                cothervar = othervarobj.cvar
                                for value in [S.Zero,pi/2,pi,-pi/2]:
                                    possiblesubs.append([(othervar,value),(sothervar,sin(value).evalf(n=30)),(sin(othervar),sin(value).evalf(n=30)), (cothervar,cos(value).evalf(n=30)), (cos(othervar),cos(value).evalf(n=30))])
                                    ishinge.append(True)
                    # all possiblesubs are present in checkzero
                    for ipossiblesub, possiblesub in enumerate(possiblesubs):
                        try:
                            eq = checkzero.subs(possiblesub).evalf(n=30)
                        except RuntimeError, e:
                            # most likely doing (1/x).subs(x,0) produces a RuntimeError (infinite recursion...)
                            log.warn(e)
                            continue
                        if not self.isValidSolution(eq):
                            continue
                        # only take the first index
                        possiblevar,possiblevalue = possiblesub[0]
                        cond = Abs(possiblevar-possiblevalue.evalf(n=30))
                        if not self.CheckExpressionUnique(handledconds, cond):
                            # already present, so don't use it for double expressions
                            continue
                        if ishinge[ipossiblesub]:
                            evalcond = Abs(fmod(possiblevar-possiblevalue+pi,2*pi)-pi)
                        else:
                            evalcond = cond
                        if eq == S.Zero:
                            log.info('c = %d, adding case %s = %s in %s', \
                                     scopecounter, possiblevar, possiblevalue, checkzero)
                            
                            # if the variable is 1 and part of the rotation matrix, can deduce other variables
                            if possiblevar in rotsymbols and (possiblevalue == S.One or possiblevalue == -S.One):
                                row1 = int(possiblevar.name[-2])
                                col1 = int(possiblevar.name[-1])
                                possiblesub.append((Symbol('%s%d%d'%(possiblevar.name[:-2], row1, (col1+1)%3)), S.Zero))
                                possiblesub.append((Symbol('%s%d%d'%(possiblevar.name[:-2], row1, (col1+2)%3)), S.Zero))
                                possiblesub.append((Symbol('%s%d%d'%(possiblevar.name[:-2], (row1+1)%3, col1)), S.Zero))
                                possiblesub.append((Symbol('%s%d%d'%(possiblevar.name[:-2], (row1+2)%3, col1)), S.Zero))
                            checkexpr = [[cond],evalcond,possiblesub, []]
                            flatzerosubstitutioneqs.append(checkexpr)
                            localsubstitutioneqs.append(checkexpr)
                            handledconds.append(cond)
                            continue
                        
                        # try another possiblesub
                        for ipossiblesub2, possiblesub2 in enumerate(possiblesubs[ipossiblesub+1:]):
                            possiblevar2,possiblevalue2 = possiblesub2[0]
                            if possiblevar == possiblevar2:
                                # same var, so skip
                                continue
                            try:
                                eq2 = eq.subs(possiblesub2).evalf(n=30)
                            except RuntimeError, e:
                                # most likely doing (1/x).subs(x,0) produces a RuntimeError (infinite recursion...)
                                log.warn(e)
                                continue
                            
                            if not self.isValidSolution(eq2):
                                continue
                            if eq2 == S.Zero:
                                possiblevar2,possiblevalue2 = possiblesub2[0]
                                cond2 = Abs(possiblevar2-possiblevalue2.evalf(n=30))
                                if not self.CheckExpressionUnique(handledconds ,cond2):
                                    # already present, so don't use it for double expressions
                                    continue
                                
                                # don't combine the conditions like cond+cond2, instead test them individually (this reduces the solution tree)
                                if ishinge[ipossiblesub+ipossiblesub2+1]:
                                    evalcond2 = Abs(fmod(possiblevar2-possiblevalue2+pi,2*pi)-pi)# + evalcond
                                else:
                                    evalcond2 = cond2# + evalcond
                                #cond2 += cond
                                if self.CheckExpressionUnique(handledconds, cond+cond2):
                                    # if the variables are both part of the rotation matrix and both zeros, can deduce other rotation variables
                                    if self._iktype == 'transform6d' and possiblevar in rotsymbols and possiblevalue == S.Zero and possiblevar2 in rotsymbols and possiblevalue2 == S.Zero:
                                        checkexpr = [[cond+cond2],evalcond+evalcond2, possiblesub+possiblesub2, []]
                                        flatzerosubstitutioneqs.append(checkexpr)
                                        localsubstitutioneqs.append(checkexpr)
                                        handledconds.append(cond+cond2)
                                        row1 = int(possiblevar.name[-2])
                                        col1 = int(possiblevar.name[-1])
                                        row2 = int(possiblevar2.name[-2])
                                        col2 = int(possiblevar2.name[-1])
                                        row3 = 3 - row1 - row2
                                        col3 = 3 - col1 - col2
                                        if row1 == row2:
                                            # (row1, col3) is either 1 or -1, but don't know which.
                                            # know that (row1+1,col3) and (row1+2,col3) are zero though...
                                            checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], (row2+1)%3, col3)), S.Zero))
                                            checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], (row1+2)%3, col3)), S.Zero))
                                            # furthermore can defer that the left over 4 values are [cos(ang), sin(ang), cos(ang), -sin(ang)] = abcd
                                            if row1 == 1:
                                                minrow = 0
                                                maxrow = 2
                                            else:
                                                minrow = (row1+1)%3
                                                maxrow = (row1+2)%3
                                            ra = Symbol('%s%d%d'%(possiblevar.name[:-2], minrow, col1))
                                            rb = Symbol('%s%d%d'%(possiblevar.name[:-2], minrow, col2))
                                            rc = Symbol('%s%d%d'%(possiblevar.name[:-2], maxrow, col1))
                                            rd = Symbol('%s%d%d'%(possiblevar.name[:-2], maxrow, col2))
                                            checkexpr[2].append((rb**2, S.One-ra**2))
                                            checkexpr[2].append((rb**3, rb-rb*ra**2)) # need 3rd power since sympy cannot divide out the square
                                            checkexpr[2].append((rc**2, S.One-ra**2))
                                            #checkexpr[2].append((rc, -rb)) # not true
                                            #checkexpr[2].append((rd, ra)) # not true
                                        elif col1 == col2:
                                            # (row3, col1) is either 1 or -1, but don't know which.
                                            # know that (row3,col1+1) and (row3,col1+2) are zero though...
                                            checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row3, (col1+1)%3)), S.Zero))
                                            checkexpr[2].append((Symbol('%s%d%d'%(possiblevar.name[:-2], row3, (col1+2)%3)), S.Zero))
                                            # furthermore can defer that the left over 4 values are [cos(ang), sin(ang), cos(ang), -sin(ang)] = abcd
                                            if col1 == 1:
                                                mincol = 0
                                                maxcol = 2
                                            else:
                                                mincol = (col1+1)%3
                                                maxcol = (col1+2)%3
                                            ra = Symbol('%s%d%d'%(possiblevar.name[:-2], row1, mincol))
                                            rb = Symbol('%s%d%d'%(possiblevar.name[:-2], row2, mincol))
                                            rc = Symbol('%s%d%d'%(possiblevar.name[:-2], row1, maxcol))
                                            rd = Symbol('%s%d%d'%(possiblevar.name[:-2], row2, maxcol))
                                            checkexpr[2].append((rb**2, S.One-ra**2))
                                            checkexpr[2].append((rb**3, rb-rb*ra**2)) # need 3rd power since sympy cannot divide out the square
                                            checkexpr[2].append((rc**2, S.One-ra**2))
                                            #checkexpr[2].append((rc, -rb)) # not true
                                            #checkexpr[2].append((rd, ra)) # not true
                                        log.info('dual constraint %s\n' + \
                                                 '	in %s', \
                                                 ("\n"+" "*24).join(str(x) for x in list(checkexpr[2])), \
                                                 checkzero)
                                    else:
                                        # shouldn't have any rotation vars
                                        if not possiblevar in rotsymbols and not possiblevar2 in rotsymbols:
                                            checkexpr = [[cond+cond2],evalcond+evalcond2, possiblesub+possiblesub2, []]
                                            flatzerosubstitutioneqs.append(checkexpr)
                                            localsubstitutioneqs.append(checkexpr)
                                            handledconds.append(cond+cond2)
                zerosubstitutioneqs[isolution] += localsubstitutioneqs
        # test the solutions
        
        # PREV: have to take the cross product of all the zerosubstitutioneqs in order to form stronger constraints on the equations because the following condition will be executed only if all SolverCheckZeros evalute to 0
        # NEW: not sure why cross product is necessary anymore....
        zerobranches = []
        accumequations = []
#         # since sequence_cross_product requires all lists to be non-empty, insert None for empty lists
#         for conditioneqs in zerosubstitutioneqs:
#             if len(conditioneqs) == 0:
#                 conditioneqs.append(None)
#         for conditioneqs in self.sequence_cross_product(*zerosubstitutioneqs):
#             validconditioneqs = [c for c in conditioneqs if c is not None]
#             if len(validconditioneqs) > 1:
#                 # merge the equations, be careful not to merge equations constraining the same variable
#                 cond = []
#                 evalcond = S.Zero
#                 othervarsubs = []
#                 dictequations = []
#                 duplicatesub = False
#                 for subcond, subevalcond, subothervarsubs, subdictequations in validconditioneqs:
#                     cond += subcond
#                     evalcond += abs(subevalcond)
#                     for subothervarsub in subothervarsubs:
#                         if subothervarsub[0] in [sym for sym,value in othervarsubs]:
#                             # variable is duplicated
#                             duplicatesub = True
#                             break
#                         othervarsubs.append(subothervarsub)
#                     if duplicatesub:
#                         break
#                     dictequations += subdictequations
#                 if not duplicatesub:
#                     flatzerosubstitutioneqs.append([cond,evalcond,othervarsubs,dictequations])
        if self._iktype == 'transform6d' or self._iktype == 'rotation3d':
            trysubstitutions = self.ppsubs+self.npxyzsubs+self.rxpsubs
        else:
            trysubstitutions = self.ppsubs
        log.debug('c = %d, %d zero-substitution(s)', scopecounter, len(flatzerosubstitutioneqs))
        
        for iflatzerosubstitutioneqs, (cond, evalcond, othervarsubs, dictequations) in enumerate(flatzerosubstitutioneqs):
            # have to convert to fractions before substituting!
            if not all([self.isValidSolution(v) for s,v in othervarsubs]):
                continue
            othervarsubs = [(s,self.ConvertRealToRationalEquation(v)) for s,v in othervarsubs]
            #NewEquations = [eq.subs(self.npxyzsubs + self.rxpsubs).subs(othervarsubs) for eq in AllEquations]
            NewEquations = [eq.subs(othervarsubs) for eq in AllEquations]
            NewEquationsClean = self.PropagateSolvedConstants(NewEquations, othersolvedvars, curvars)
            
            try:
                # forcing a value, so have to check if all equations in NewEquations that do not contain
                # unknown variables are really 0
                extrazerochecks=[]
                for i in range(len(NewEquations)):
                    expr = NewEquations[i]
                    if not self.isValidSolution(expr):
                        log.warn('not valid: %s',expr)
                        extrazerochecks=None
                        break
                    if not expr.has(*allvars) and self.CheckExpressionUnique(extrazerochecks,expr):
                        if expr.is_Symbol:
                            # can set that symbol to zero and create a new set of equations!
                            extrazerochecks.append(expr.subs(solsubs).evalf(n=30))
                if extrazerochecks is not None:
                    newcases = set(currentcases)
                    for singlecond in cond:
                        newcases.add(singlecond)                            
                    if not self.degeneratecases.CheckCases(newcases):
                        log.info('depth = %d, c = %d, iter = %d/%d\n' +\
                                 '        start new cases: %s', \
                                 len(currentcases), scopecounter, iflatzerosubstitutioneqs, len(flatzerosubstitutioneqs), \
                                 ("\n"+" "*25).join(str(x) for x in list(newcases)))
                        
                        if len(NewEquationsClean) > 0:
                            newcasesubs = currentcasesubs+othervarsubs
                            self.globalsymbols = []
                            for casesub in newcasesubs:
                                self._AddToGlobalSymbols(casesub[0], casesub[1])
                            extradictequations = []
                            for s,v in trysubstitutions:
                                neweq = v.subs(newcasesubs)
                                if neweq != v:
                                    # should we make sure we're not adding it a second time?
                                    newcasesubs.append((s, neweq))
                                    extradictequations.append((s, neweq))
                                    self._AddToGlobalSymbols(s, neweq)
                            for var, eq in chain(originalGlobalSymbols, dictequations):
                                neweq = eq.subs(othervarsubs)
                                if not self.isValidSolution(neweq):
                                    raise self.CannotSolveError('equation %s is invalid because of the following substitutions: %s'%(eq, othervarsubs))
                                
                                if neweq == S.Zero:
                                    extradictequations.append((var, S.Zero))
                                self._AddToGlobalSymbols(var, neweq)
                            if len(extradictequations) > 0:
                                # have to re-substitute since some equations evaluated to zero
                                NewEquationsClean = [eq.subs(extradictequations).expand() for eq in NewEquationsClean]
                            newtree = self.SolveAllEquations(NewEquationsClean,curvars,othersolvedvars,solsubs,endbranchtree,currentcases=newcases, currentcasesubs=newcasesubs, unknownvars=unknownvars)
                            accumequations.append(NewEquationsClean) # store the equations for debugging purposes
                        else:
                            log.info('no new equations! probably can freely determine %r', curvars)
                            # unfortunately cannot add as a FreeVariable since all the left over variables will have complex dependencies
                            # therefore, iterate a couple of jointevals
                            newtree = []
                            for curvar in curvars:
                                newtree.append(AST.SolverSolution(curvar.name, jointeval=[S.Zero,pi/2,pi,-pi/2], isHinge=self.IsHinge(curvar.name)))
                            newtree += endbranchtree
                        zerobranches.append(([evalcond]+extrazerochecks,newtree,dictequations)) # what about extradictequations?

                        log.info('depth = %d, c = %d, iter = %d/%d\n' \
                                 + '        add new cases: %s', \
                                 len(currentcases), scopecounter, iflatzerosubstitutioneqs, len(flatzerosubstitutioneqs), \
                                 ("\n"+" "*23).join(str(x) for x in list(newcases)))
                        self.degeneratecases.AddCases(newcases)
                    else:
                        log.warn('already has handled cases %r', newcases)                        
            except self.CannotSolveError, e:
                log.debug(e)
                continue
            finally:
                # restore the global symbols
                self.globalsymbols = originalGlobalSymbols
                
        if len(zerobranches) > 0:
            branchconds = AST.SolverBranchConds(zerobranches+[(None,[AST.SolverBreak('branch miss %r'%curvars, [(var,self._SubstituteGlobalSymbols(eq, originalGlobalSymbols)) for var, eq in currentcasesubs], othersolvedvars, solsubs, originalGlobalSymbols, endbranchtree)],[])])
            branchconds.accumequations = accumequations
            lastbranch.append(branchconds)
        else:            
            # add GuessValuesAndSolveEquations?
            lastbranch.append(AST.SolverBreak('no branches %r'%curvars, [(var,self._SubstituteGlobalSymbols(eq, originalGlobalSymbols)) for var, eq in currentcasesubs], othersolvedvars, solsubs, originalGlobalSymbols, endbranchtree))
            
        return prevbranch
    
    def GuessValuesAndSolveEquations(self, AllEquations, curvars, othersolvedvars, solsubs, endbranchtree, currentcases=None, unknownvars=None, currentcasesubs=None):
        # perhaps there's a degree of freedom that is not trivial to compute?
        # take the highest hinge variable and set it
        scopecounter = int(self._scopecounter)
        hingevariables = [curvar for curvar in sorted(curvars,reverse=True) if self.IsHinge(curvar.name)]
        if len(hingevariables) > 0 and len(curvars) >= 2:
            curvar = hingevariables[0]
            leftovervars = list(curvars)
            leftovervars.remove(curvar)
            newtree = [AST.SolverConditionedSolution([])]
            zerovalues = []
            for jointeval in [S.Zero,pi/2,pi,-pi/2]:
                checkzeroequations = []
                NewEquations = []
                for eq in AllEquations:
                    neweq = eq.subs(curvar, jointeval)
                    neweqeval = neweq.evalf()
                    if neweq.is_number:
                        # if zero, then can ignore
                        if neweq == S.Zero:
                            continue
                        # if not zero, then a contradiciton, so jointeval is bad
                        NewEquations = None
                        break                        
                    if neweq.has(*leftovervars):
                        NewEquations.append(neweq)
                    else:
                        checkzeroequations.append(neweq)
                if NewEquations is None:
                    continue
                # check to make sure all leftover vars are in scope
                cansolve = True
                for leftovervar in leftovervars:
                    if not any([eq.has(leftovervar) for eq in NewEquations]):
                        cansolve = False
                        break
                if not cansolve:
                    continue
                if len(checkzeroequations) > 0:
                    solution = AST.SolverSolution(curvar.name, jointeval=[jointeval], isHinge=self.IsHinge(curvar.name))
                    solution.checkforzeros = checkzeroequations
                    solution.FeasibleIsZeros = True
                    newtree[0].solversolutions.append(solution)
                else:
                    # one value is enough
                    zerovalues.append(jointeval)
            if len(zerovalues) > 0:
                # prioritize these solutions since they don't come with any extra checks
                solution = AST.SolverSolution(curvar.name, jointeval=zerovalues, isHinge=self.IsHinge(curvar.name))
                solution.FeasibleIsZeros = True
                newtree = [solution]
            elif len(newtree[0].solversolutions) == 0:
                # nothing found so remove the condition node
                newtree = []
            if len(newtree) > 0:
                log.warn('c=%d, think there is a free variable, but cannot solve relationship, so setting variable %s', scopecounter, curvar)
                newtree += self.SolveAllEquations(AllEquations, leftovervars, othersolvedvars+[curvar], solsubs+self.Variable(curvar).subs, endbranchtree,currentcases=currentcases, currentcasesubs=currentcasesubs, unknownvars=unknownvars)
                return newtree

        if len(curvars) == 1:
            log.info('have only one variable left %r and most likely it is not in equations %r', curvars[0], AllEquations)
            solution = AST.SolverSolution(curvars[0].name, jointeval=[S.Zero], isHinge=self.IsHinge(curvars[0].name))
            solution.FeasibleIsZeros = True
            return [solution]+endbranchtree
        
        raise self.CannotSolveError('cannot find a good variable')
    
    def SolvePairVariablesHalfAngle(self,raweqns,var0,var1,othersolvedvars,subs=None):
        """solves equations of two variables in sin and cos
        """
        from ikfast_AST import AST
        varsym0 = self.Variable(var0)
        varsym1 = self.Variable(var1)
        varsyms = [varsym0,varsym1]
        unknownvars=[varsym0.cvar,varsym0.svar,varsym1.cvar,varsym1.svar]
        varsubs=varsym0.subs+varsym1.subs
        varsubsinv = varsym0.subsinv+varsym1.subsinv
        halftansubs = []
        for varsym in varsyms:
            halftansubs += [(varsym.cvar,(1-varsym.htvar**2)/(1+varsym.htvar**2)),(varsym.svar,2*varsym.htvar/(1+varsym.htvar**2))]
        dummyvars = []
        for othervar in othersolvedvars:
            v = self.Variable(othervar)
            dummyvars += [v.cvar,v.svar,v.var,v.htvar]
            
        polyeqs = []
        for eq in raweqns:
            trigsubs = [(varsym0.svar**2,1-varsym0.cvar**2), (varsym0.svar**3,varsym0.svar*(1-varsym0.cvar**2)), (varsym1.svar**2,1-varsym1.cvar**2), (varsym1.svar**3,varsym1.svar*(1-varsym1.cvar**2))]
            peq = Poly(eq.subs(varsubs).subs(trigsubs).expand().subs(trigsubs),*unknownvars)
            if peq.has(varsym0.var) or peq.has(varsym1.var):
                raise self.CannotSolveError('expecting only sin and cos! %s'%peq)
            
            maxmonoms = [0,0,0,0]
            maxdenom = [0,0]
            for monoms in peq.monoms():
                for i in range(4):
                    maxmonoms[i] = max(maxmonoms[i],monoms[i])
                maxdenom[0] = max(maxdenom[0],monoms[0]+monoms[1])
                maxdenom[1] = max(maxdenom[1],monoms[2]+monoms[3])
            eqnew = S.Zero
            for monoms,c in peq.terms():
                term = c
                for i in range(4):
                    num,denom = fraction(halftansubs[i][1])
                    term *= num**monoms[i]
                # the denoms for 0,1 and 2,3 are the same
                for i in [0,2]:
                    denom = fraction(halftansubs[i][1])[1]
                    term *= denom**(maxdenom[i/2]-monoms[i]-monoms[i+1])
                complexityvalue = self.codeComplexity(term.expand())
                if complexityvalue < 450:
                    eqnew += simplify(term)
                else:
                    # too big, so don't simplify?
                    eqnew += term
            polyeq = Poly(eqnew,varsym0.htvar,varsym1.htvar)
            if polyeq.TC() == S.Zero:
                # might be able to divide out variables?
                minmonoms = None
                for monom in polyeq.monoms():
                    if minmonoms is None:
                        minmonoms = list(monom)
                    else:
                        for i in range(len(minmonoms)):
                            minmonoms[i] = min(minmonoms[i],monom[i])
                newpolyeq = Poly(S.Zero,*polyeq.gens)
                for m,c in polyeq.terms():
                    newm = list(m)
                    for i in range(len(minmonoms)):
                        newm[i] -= minmonoms[i]
                    newpolyeq = newpolyeq.add(Poly.from_dict({tuple(newm):c},*newpolyeq.gens))
                log.warn('converting polyeq "%s" to "%s"'%(polyeq,newpolyeq))
                # check if any equations are only in one variable
                polyeq = newpolyeq                
            polyeqs.append(polyeq)
            
        try:
            return self.solveSingleVariable(self.sortComplexity([e.as_expr() for e in polyeqs if not e.has(varsym1.htvar)]),varsym0.var,othersolvedvars,unknownvars=[])
        except self.CannotSolveError:
            pass
        try:
            return self.solveSingleVariable(self.sortComplexity([e.as_expr() for e in polyeqs if not e.has(varsym0.htvar)]),varsym1.var,othersolvedvars,unknownvars=[])
        except self.CannotSolveError:
            pass
        
        complexity = [(self.codeComplexity(peq.as_expr()),peq) for peq in polyeqs]
        complexity.sort(key=itemgetter(0))
        polyeqs = [peq[1] for peq in complexity]
        
        solutions = [None,None]
        linearsolution = None
        for ileftvar in range(2):
            if linearsolution is not None:
                break
            leftvar = varsyms[ileftvar].htvar
            newpolyeqs = [Poly(eq,varsyms[1-ileftvar].htvar) for eq in polyeqs]
            mindegree = __builtin__.min([max(peq.degree_list()) for peq in newpolyeqs])
            maxdegree = __builtin__.max([max(peq.degree_list()) for peq in newpolyeqs])
            for peq in newpolyeqs:
                if len(peq.monoms()) == 1:
                    possiblefinaleq = self.checkFinalEquation(Poly(peq.LC(),leftvar),subs)
                    if possiblefinaleq is not None:
                        solutions[ileftvar] = [possiblefinaleq]
                        break
            for degree in range(mindegree,maxdegree+1):
                if solutions[ileftvar] is not None or linearsolution is not None:
                    break
                newpolyeqs2 = [peq for peq in newpolyeqs if max(peq.degree_list()) <= degree]
                if degree+1 <= len(newpolyeqs2):
                    # in order to avoid wrong solutions, have to get resultants for all equations
                    possibilities = []
                    unusedindices = range(len(newpolyeqs2))
                    for eqsindices in combinations(range(len(newpolyeqs2)),degree+1):
                        Mall = zeros((degree+1,degree+1))
                        totalcomplexity = 0
                        for i,eqindex in enumerate(eqsindices):
                            eq = newpolyeqs2[eqindex]
                            for j,c in eq.terms():
                                totalcomplexity += self.codeComplexity(c.expand())
                                Mall[i,j[0]] = c
                        if degree >= 4 and totalcomplexity > 5000:
                            # the determinant will never finish otherwise
                            continue
                        # det_bareis freezes when there are huge fractions
                        #det=self.det_bareis(Mall,*(self.pvars+dummyvars+[leftvar]))
#                         for i in range(Mall.shape[0]):
#                             for j in range(Mall.shape[1]):
#                                 Mall[i,j] = Poly(Mall[i,j],leftvar)
                        try:
                            Malldet = Mall.berkowitz_det()
                        except Exception, e:
                            log.warn('failed to compute determinant: %s', e)
                            continue
                        
                        complexity = self.codeComplexity(Malldet)
                        if complexity > 1200:
                            log.warn('determinant complexity is too big %d', complexity)
                            continue
                        possiblefinaleq = self.checkFinalEquation(Poly(Malldet,leftvar),subs)
                        if possiblefinaleq is not None:
                            # sometimes +- I are solutions, so remove them
                            q,r = div(possiblefinaleq,leftvar+I)
                            if r == S.Zero:
                                possiblefinaleq = Poly(q,leftvar)
                            q,r = div(possiblefinaleq,leftvar-I)
                            if r == S.Zero:
                                possiblefinaleq = Poly(q,leftvar)
                            possibilities.append(possiblefinaleq)
                            for eqindex in eqsindices:
                                if eqindex in unusedindices:
                                    unusedindices.remove(eqindex)
                            if len(unusedindices) == 0:
                                break
                    if len(possibilities) > 0:
                        if len(possibilities) > 1:
                            try:
                                linearsolutions = self.solveVariablesLinearly(possibilities,othersolvedvars)
                                # if can solve for a unique solution linearly, then prioritize this over anything
                                prevsolution = AST.SolverBreak('SolvePairVariablesHalfAngle fail')
                                for divisor,linearsolution in linearsolutions:
                                    assert(len(linearsolution)==1)
                                    divisorsymbol = self.gsymbolgen.next()
                                    solversolution = AST.SolverSolution(varsyms[ileftvar].name,jointeval=[2*atan(linearsolution[0]/divisorsymbol)],isHinge=self.IsHinge(varsyms[ileftvar].name))
                                    prevsolution = AST.SolverCheckZeros(varsyms[ileftvar].name,[divisorsymbol],zerobranch=[prevsolution],nonzerobranch=[solversolution],thresh=1e-6)
                                    prevsolution.dictequations = [(divisorsymbol,divisor)]
                                linearsolution = prevsolution
                                break
                            
                            except self.CannotSolveError:
                                pass
                            
                        # sort with respect to degree
                        equationdegrees = [(max(peq.degree_list())*100000+self.codeComplexity(peq.as_expr()),peq) for peq in possibilities]
                        equationdegrees.sort(key=itemgetter(0))
                        solutions[ileftvar] = [peq[1] for peq in equationdegrees]
                        break
                    
        if linearsolution is not None:
            return [linearsolution]
        
        # take the solution with the smallest degree
        pfinals = None
        ileftvar = None
        if solutions[0] is not None:
            if solutions[1] is not None:
                if max(solutions[1][0].degree_list()) < max(solutions[0][0].degree_list()):
                    pfinals = solutions[1]
                    ileftvar = 1
                elif max(solutions[1][0].degree_list()) == max(solutions[0][0].degree_list()) and self.codeComplexity(solutions[1][0].as_expr()) < self.codeComplexity(solutions[0][0].as_expr()):
                    pfinals = solutions[1]
                    ileftvar = 1
                else:
                    pfinals = solutions[0]
                    ileftvar = 0
            else:
                pfinals = solutions[0]
                ileftvar = 0
        elif solutions[1] is not None:
            pfinals = solutions[1]
            ileftvar = 1
        
        dictequations = []
        if pfinals is None:
            #simplifyfn = self._createSimplifyFn(self.freejointvars,self.freevarsubs,self.freevarsubsinv)
            for newreducedeqs in combinations(polyeqs,2):
                try:
                    Mall = None
                    numrepeating = None
                    for ileftvar in range(2):
                        # TODO, sometimes this works and sometimes this doesn't
                        try:
                            Mall, allmonoms = self.solveDialytically(newreducedeqs,ileftvar,returnmatrix=True)
                            if Mall is not None:
                                leftvar=polyeqs[0].gens[ileftvar]
                                break
                        except self.CannotSolveError, e:
                            log.debug(e)
                        
                    if Mall is None:
                        continue
                    
                    shape=Mall[0].shape
                    assert(shape[0] == 4 and shape[1] == 4)
                    Malltemp = [None]*len(Mall)
                    M = zeros(shape)
                    for idegree in range(len(Mall)):
                        Malltemp[idegree] = zeros(shape)
                        for i in range(shape[0]):
                            for j in range(shape[1]):
                                if Mall[idegree][i,j] != S.Zero:
                                    if self.codeComplexity(Mall[idegree][i,j])>5:
                                        sym = self.gsymbolgen.next()
                                        Malltemp[idegree][i,j] = sym
                                        dictequations.append((sym,Mall[idegree][i,j]))
                                    else:
                                        Malltemp[idegree][i,j] = Mall[idegree][i,j]
                        M += Malltemp[idegree]*leftvar**idegree
                    tempsymbols = [self.gsymbolgen.next() for i in range(16)]
                    tempsubs = []
                    for i in range(16):
                        if M[i] != S.Zero:
                            tempsubs.append((tempsymbols[i],Poly(M[i],leftvar)))
                        else:
                            tempsymbols[i] = S.Zero
                    Mtemp = Matrix(4,4,tempsymbols)                    
                    dettemp=Mtemp.det()
                    log.info('multiplying all determinant coefficients for solving %s',leftvar)
                    eqadds = []
                    for arg in dettemp.args:
                        eqmuls = [Poly(arg2.subs(tempsubs),leftvar) for arg2 in arg.args]
                        if sum(eqmuls[0].degree_list()) == 0:
                            eq = eqmuls.pop(0)
                            eqmuls[0] = eqmuls[0]*eq
                        while len(eqmuls) > 1:
                            ioffset = 0
                            eqmuls2 = []
                            while ioffset < len(eqmuls)-1:
                                eqmuls2.append(eqmuls[ioffset]*eqmuls[ioffset+1])
                                ioffset += 2
                            eqmuls = eqmuls2
                        eqadds.append(eqmuls[0])
                    log.info('done multiplying all determinant, now convert to poly')
                    det = Poly(S.Zero,leftvar)
                    for ieq, eq in enumerate(eqadds):
                        log.info('adding to det %d/%d', ieq, len(eqadds))
                        det += eq
                    if len(Mall) <= 3:
                        # need to simplify further since self.globalsymbols can have important substitutions that can yield the entire determinant to zero
                        log.info('attempting to simplify determinant...')
                        newdet = Poly(S.Zero,leftvar)
                        for m,c in det.terms():
                            origComplexity = self.codeComplexity(c)
                            # 100 is a guess
                            if origComplexity < 100:
                                neweq = c.subs(dictequations)
                                if self.codeComplexity(neweq) < 100:
                                    neweq = self._SubstituteGlobalSymbols(neweq).expand()
                                    newComplexity = self.codeComplexity(neweq)
                                    if newComplexity < origComplexity:
                                        c = neweq
                            newdet += c*leftvar**m[0]
                        det = newdet
                    if det.degree(0) <= 0:
                        continue
                    pfinals = [det]
                    break
                except self.CannotSolveError,e:
                    log.debug(e)
                    
        if pfinals is None:
            raise self.CannotSolveError('SolvePairVariablesHalfAngle: failed to solve dialytically with %d equations'%(len(polyeqs)))
        
        jointsol = 2*atan(varsyms[ileftvar].htvar)
        solution = AST.SolverPolynomialRoots(jointname=varsyms[ileftvar].name,poly=pfinals[0],jointeval=[jointsol],isHinge=self.IsHinge(varsyms[ileftvar].name))
        solution.checkforzeros = []
        solution.postcheckforzeros = []
        if len(pfinals) > 1:
            # verify with at least one solution
            solution.postcheckfornonzeros = [peq.as_expr() for peq in pfinals[1:2]]
            solution.polybackup = pfinals[1]
        solution.postcheckforrange = []
        solution.dictequations = dictequations
        solution.postcheckfornonzerosThresh = 1e-7 # make threshold a little loose since can be a lot of numbers compounding. depending on the degree, can expect small coefficients to be still valid
        solution.AddHalfTanValue = True
        return [solution]

    def _createSimplifyFn(self,vars,varsubs,varsubsinv):
        return lambda eq: self.trigsimp(eq.subs(varsubsinv),vars).subs(varsubs)
                
    def solveVariablesLinearly(self,polyeqs,othersolvedvars,maxsolvabledegree=4):
        log.debug('solvevariables=%r, othersolvedvars=%r',polyeqs[0].gens,othersolvedvars)
        nummonoms = [len(peq.monoms())-int(peq.TC()!=S.Zero) for peq in polyeqs]
        mindegree = __builtin__.min(nummonoms)
        maxdegree = min(__builtin__.max(nummonoms),len(polyeqs))
        complexity = [(self.codeComplexity(peq.as_expr()),peq) for peq in polyeqs]
        complexity.sort(key=itemgetter(0))
        polyeqs = [peq[1] for peq in complexity]
        trigsubs = []
        trigsubsinv = []
        othersolvedvarssyms = []
        for othervar in othersolvedvars:
            v = self.Variable(othervar)
            othersolvedvarssyms += v.vars
            trigsubs += v.subs
            trigsubsinv += v.subsinv
        symbolscheck = []
        for i,solvevar in enumerate(polyeqs[0].gens):
            monom = [0]*len(polyeqs[0].gens)
            monom[i] = 1
            symbolscheck.append(tuple(monom))
        solutions = []
        for degree in range(mindegree,maxdegree+1):
            allindices = [i for i,n in enumerate(nummonoms) if n <= degree]
            if len(allindices) >= degree:
                allmonoms = set()
                for index in allindices:
                    allmonoms = allmonoms.union(set(polyeqs[index].monoms()))
                allmonoms = list(allmonoms)
                allmonoms.sort()
                if __builtin__.sum(allmonoms[0]) == 0:
                    allmonoms.pop(0)
                # allmonoms has to have symbols as a single variable
                if not all([check in allmonoms for check in symbolscheck]):
                    continue
                
                if len(allmonoms) == degree:
                    if degree > maxsolvabledegree:
                        log.warn('cannot handle linear solving for more than 4 equations')
                        continue
                    
                    systemequations = []
                    consts = []
                    for index in allindices:
                        pdict = polyeqs[index].as_dict()
                        systemequations.append([pdict.get(monom,S.Zero) for monom in allmonoms])
                        consts.append(-polyeqs[index].TC())
                    # generate at least two solutions in case first's determinant is 0
                    solutions = []
                    for startrow in range(len(systemequations)):
                        rows = [startrow]
                        M = Matrix(1,len(allmonoms),systemequations[rows[0]])
                        for i in range(startrow+1,len(systemequations)):
                            numequationsneeded = M.shape[1] - M.shape[0]
                            if i+numequationsneeded > len(systemequations):
                                # cannot do anything
                                break
                            mergedsystemequations = list(systemequations[i])
                            for j in range(1,numequationsneeded):
                                mergedsystemequations += systemequations[i+j]
                            M2 = M.col_join(Matrix(numequationsneeded,len(allmonoms),mergedsystemequations))
                            complexity = 0
                            for i2 in range(M2.rows):
                                for j2 in range(M2.cols):
                                    complexity += self.codeComplexity(M2[i,j])
                            if self.IsDeterminantNonZeroByEval(M2):
                                if complexity < 5000:
                                    Mdet = M2.det()
                                    if Mdet != S.Zero:
                                        M = M2
                                        for j in range(numequationsneeded):
                                            rows.append(i+j)
                                        break
                                else:
                                    log.warn('found solution, but matrix is too complex and determinant will most likely freeze (%d)', complexity)
                                
                        if M.shape[0] == M.shape[1]:
                            Mdet = self.trigsimp(Mdet.subs(trigsubsinv),othersolvedvars).subs(trigsubs)
                            #Minv = M.inv()
                            B = Matrix(M.shape[0],1,[consts[i] for i in rows])
                            Madjugate = M.adjugate()
                            solution = []
                            for check in symbolscheck:
                                value = Madjugate[allmonoms.index(check),:]*B
                                solution.append(self.trigsimp(value[0].subs(trigsubsinv),othersolvedvars).subs(trigsubs))
                            solutions.append([Mdet,solution])
                            if len(solutions) >= 2:
                                break
                    if len(solutions) > 0:
                        break                    
        if len(solutions) == 0:
            raise self.CannotSolveError('solveVariablesLinearly failed')
        
        return solutions

    def solveSingleVariableLinearly(self,raweqns,solvevar,othervars,maxnumeqs=2,douniquecheck=True):
        """
        Solves a linear system for one variable, assuming everything else is constant.

        Need >=3 equations.
        """
        cvar = Symbol('c%s' % solvevar.name)
        svar = Symbol('s%s' % solvevar.name)
        varsubs = [(cos(solvevar), cvar), (sin(solvevar), svar)]
        othervarsubs = [(sin(v)**2, 1-cos(v)**2) for v in othervars]
        eqpolys = [Poly(eq.subs(varsubs), cvar, svar) for eq in raweqns]
        eqpolys = [eq for eq in eqpolys if sum(eq.degree_list()) == 1 and not eq.TC().has(solvevar)]
        #eqpolys.sort(lambda x,y: iksolver.codeComplexity(x) - iksolver.codeComplexity(y))
        partialsolutions = []
        neweqs = []
        for p0,p1 in combinations(eqpolys, 2):
            p0dict = p0.as_dict()
            p1dict = p1.as_dict()

            M = Matrix(2, 3, \
                       [p0dict.get((1,0), S.Zero), \
                        p0dict.get((0,1), S.Zero), p0.TC(), \
                        p1dict.get((1,0), S.Zero), \
                        p1dict.get((0,1), S.Zero), p1.TC()])
            M = M.subs(othervarsubs).expand()
            
            partialsolution = [-M[1,1]*M[0,2]+M[0,1]*M[1,2], \
                               M[1,0]*M[0,2]-M[0,0]*M[1,2] , \
                               M[0,0]*M[1,1]-M[0,1]*M[1,0]]
            
            partialsolution = [eq.expand().subs(othervarsubs).expand() for eq in partialsolution]
            rank = [self.codeComplexity(eq) for eq in partialsolution]
            partialsolutions.append([rank, partialsolution])
            # cos(A)**2 + sin(A)**2 - 1 = 0, useful equation but the squares introduce wrong solutions
            #neweqs.append(partialsolution[0]**2+partialsolution[1]**2-partialsolution[2]**2)
        # try to cross
        partialsolutions.sort(lambda x, y: int(min(x[0])-min(y[0])))
        for (rank0,ps0),(rank1,ps1) in combinations(partialsolutions,2):
            if self.equal(ps0[0]*ps1[2]-ps1[0]*ps0[2],S.Zero):
                continue
            neweqs.append(ps0[0]*ps1[2]-ps1[0]*ps0[2])
            neweqs.append(ps0[1]*ps1[2]-ps1[1]*ps0[2])
            # probably a linear combination of the first two
            #neweqs.append(ps0[0]*ps1[1]-ps1[0]*ps0[1])
            # too long
            #neweqs.append(ps0[0]*ps1[0]+ps0[1]*ps1[1]-ps0[2]*ps1[2])
            if len(neweqs) >= maxnumeqs:
                break;
            
        neweqs2 = [eq.expand().subs(othervarsubs).expand() for eq in neweqs]
        
        if douniquecheck:
            reducedeqs = []
            i = 0
            while i < len(neweqs2):
                reducedeq = self.removecommonexprs(neweqs2[i])
                if neweqs2[i] != S.Zero and \
                   self.CheckExpressionUnique(reducedeqs, reducedeq):
                    reducedeqs.append(reducedeq)
                    i += 1
                else:
                    eq = neweqs2.pop(i)
        return neweqs2

    def solveHighDegreeEquationsHalfAngle(self, lineareqs, varsym, subs = None):
        """solve a set of equations in one variable with half-angle substitution
        """
        from ikfast_AST import AST
        
        dummysubs = [(varsym.cvar, (1-varsym.htvar**2)/(1+varsym.htvar**2)), \
                     (varsym.svar,      2*varsym.htvar/(1+varsym.htvar**2))]
        polyeqs = []
        
        for eq in lineareqs:
            trigsubs = [(varsym.svar**2, 1-varsym.cvar**2), \
                        (varsym.svar**3, varsym.svar*(1-varsym.cvar**2))]
            try:
                peq = Poly(eq.subs(varsym.subs).subs(trigsubs), varsym.cvar, varsym.svar)
                
            except PolynomialError, e:
                raise self.CannotSolveError('solveHighDegreeEquationsHalfAngle: poly error (%r)' % eq)
            
            if peq.has(varsym.var):
                raise self.CannotSolveError('solveHighDegreeEquationsHalfAngle: expecting only sin and cos! %s' % peq)
            
            if sum(peq.degree_list()) == 0:
                continue
            
            # check if all terms are multiples of cos/sin
            maxmonoms = [0, 0]
            maxdenom = 0
            for monoms in peq.monoms():
                for i in range(2):
                    maxmonoms[i] = max(maxmonoms[i], monoms[i])
                maxdenom = max(maxdenom,monoms[0] + monoms[1])
            eqnew = S.Zero
            for monoms,c in peq.terms():
                if c.evalf() != S.Zero: # big fractions might make this difficult to reduce to 0
                    term = c
                    for i in range(2):
                        num,denom = fraction(dummysubs[i][1])
                        term *= num**monoms[i]
                    # the denoms for 0,1 and 2,3 are the same
                    denom = fraction(dummysubs[0][1])[1]
                    term *= denom**(maxdenom-monoms[0]-monoms[1])
                    eqnew += simplify(term)
            polyeqs.append(Poly(eqnew, varsym.htvar))

        for peq in polyeqs:
            # do some type of resultants, for now just choose first polynomial
            finaleq = simplify(peq.as_expr()).expand()
            pfinal = Poly(self.removecommonexprs(finaleq, \
                                                 onlygcd = False,\
                                                 onlynumbers=True), \
                          varsym.htvar)
            pfinal = self.checkFinalEquation(pfinal,subs)
            if pfinal is not None and pfinal.degree(0) > 0:
                jointsol = 2*atan(varsym.htvar)
                solution = AST.SolverPolynomialRoots(jointname = varsym.name, \
                                                     poly = pfinal, \
                                                     jointeval = [jointsol], \
                                                     isHinge = self.IsHinge(varsym.name))
                solution.AddHalfTanValue      = True
                solution.checkforzeros        = []
                solution.postcheckforzeros    = []
                solution.postcheckfornonzeros = []
                solution.postcheckforrange    = []
                return solution

        raise self.CannotSolveError(('half-angle substitution for joint %s failed, ' + \
                                    '%d equations examined') % (varsym.var, len(polyeqs)))

    def checkFinalEquation(self, pfinal, subs = None):
        """check an equation in one variable for validity
        """
        assert(len(pfinal.gens)==1)
        if subs is None:
            subs = []
        htvar = pfinal.gens[0]
        # remove all trivial 0s
        while sum(pfinal.degree_list()) > 0 and pfinal.TC() == S.Zero:
            pfinalnew = Poly(S.Zero,htvar)
            for m,c in pfinal.terms():
                if m[0] > 0:
                    pfinalnew += c*htvar**(m[0]-1)
            pfinal = pfinalnew
        # check to see that LC is non-zero for at least one solution
        if pfinal.LC().evalf() == S.Zero or all([pfinal.LC().subs(subs).subs(self.globalsymbols).subs(testconsistentvalue).evalf()==S.Zero for testconsistentvalue in self.testconsistentvalues]):
            return None
        
        # sanity check that polynomial can produce a solution and is not actually very small values
        found = False
        LCnormalized, common = self.removecommonexprs(pfinal.LC(),returncommon=True,onlygcd=False,onlynumbers=True)
        pfinaldict = pfinal.as_dict()
        for testconsistentvalue in self.testconsistentvalues:
            coeffs = []
            globalsymbols = [(s,v.subs(self.globalsymbols).subs(testconsistentvalue).evalf()) for s,v in self.globalsymbols]
            for degree in range(pfinal.degree(0),-1,-1):
                value = pfinaldict.get((degree,),S.Zero).subs(subs).subs(globalsymbols+testconsistentvalue).evalf()/common.evalf()
                if value.has(I): # check if has imaginary number
                    coeffs = None
                    break
                coeffs.append(value)
                # since coeffs[0] is normalized with the LC constant, can compare for precision
                if len(coeffs) == 1 and Abs(coeffs[0]) < 2*(10.0**-self.precision):
                    coeffs = None
                    break
            if coeffs is None:
                continue
            if not all([c.is_number for c in coeffs]):
                # cannot evalute
                log.warn('cannot evaluate\n        %s', \
                         "\n        ".join(str(x) for x in coeffs))
                found = True
                break            
            realsolution = pfinal.gens[0].subs(subs).subs(self.globalsymbols).subs(testconsistentvalue).evalf()
            # need to convert to float64 first, X.evalf() is still a sympy object
            roots = mpmath.polyroots(numpy.array(numpy.array(coeffs),numpy.float64))
            for root in roots:
                if Abs(float(root.imag)) < 10.0**-self.precision and Abs(float(root.real)-realsolution) < 10.0**-(self.precision-2):
                    found = True
                    break
            if found:
                break
        return pfinal if found else None

    def solveSingleVariable(self, raweqns, \
                            var, othersolvedvars, \
                            maxsolutions = 4, maxdegree = 2, \
                            subs = None, \
                            unknownvars = None):
        
        from ikfast_AST import AST
        varsym = self.Variable(var)
        vars = [varsym.cvar, varsym.svar, varsym.htvar, var]
        othersubs = []
        for othersolvedvar in othersolvedvars:
            othersubs += self.Variable(othersolvedvar).subs

#         eqns = []
#         for eq in raweqns:
#             if eq.has(*vars):
#                 # for equations that are very complex, make sure at least one set of values yields a non zero equation
#                 testeq = eq.subs(varsym.subs+othersubs)
#                 if any([testeq.subs(testconsistentvalue).evalf()!=S.Zero for testconsistentvalue in self.testconsistentvalues]):
#                     eqns.append(eq)
        eqns = [eq.expand() for eq in raweqns if eq.has(*vars)]
        if len(eqns) == 0:
            raise self.CannotSolveError('not enough equations')

        # prioritize finding a solution when var is alone
        returnfirstsolutions = []
        
        for eq in eqns:
            symbolgen = cse_main.numbered_symbols('const')
            eqnew, symbols = self.groupTerms(eq.subs(varsym.subs), vars, symbolgen)
            try:
                ps = Poly(eqnew, varsym.svar)
                pc = Poly(eqnew, varsym.cvar)
                if sum(ps.degree_list()) > 0 or \
                   sum(pc.degree_list()) > 0 or \
                   ps.TC() == S.Zero or \
                   pc.TC() == S.Zero:
                    continue
                
            except PolynomialError:
                continue
            
            numvar = self.countVariables(eqnew, var)
            if numvar in [1, 2]:
                try:
                    tempsolutions  = solve(eqnew, var)

                    # TGN: ensure curvars is a subset of self.trigvars_subs
                    assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                    # equivalent?
                    assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                    
                    jointsolutions = [self.SimplifyTransform(self.trigsimp_new(s.subs(symbols))) \
                                      for s in tempsolutions]
                    if all([self.isValidSolution(s) and s != S.Zero \
                            for s in jointsolutions]) and \
                                len(jointsolutions) > 0:
                        # check if any solutions don't have divide by zero problems
                        returnfirstsolutions.append(AST.SolverSolution(var.name, \
                                                                       jointeval = jointsolutions,\
                                                                       isHinge = self.IsHinge(var.name)))
                        hasdividebyzero = any([len(self.checkForDivideByZero(self._SubstituteGlobalSymbols(s))) > 0 \
                                               for s in jointsolutions])
                        if not hasdividebyzero:
                            return returnfirstsolutions
                        
                except NotImplementedError, e:
                    # when solve cannot solve an equation
                    log.warn(e)
            
            numvar = self.countVariables(eqnew, varsym.htvar)
            if Poly(eqnew, varsym.htvar).TC() != S.Zero and numvar in [1, 2]:
                try:
                    tempsolutions = solve(eqnew,varsym.htvar)
                    jointsolutions = []
                    for s in tempsolutions:
                        s2 = s.subs(symbols)

                        # TGN: ensure curvars is a subset of self.trigvars_subs
                        assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                        # equivalent?
                        assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                    
                        s3 = self.trigsimp_new(s2)
                        s4 = self.SimplifyTransform(s3)
                        try:
                            jointsolutions.append(2*atan(s4, evaluate = False))
                            # set evaluate to False; otherwise it takes long time to evaluate when s4 is a number
                        except RuntimeError, e:
                            log.warn('got runtime error when taking atan: %s', e)
                            
                    if all([self.isValidSolution(s) \
                            and s != S.Zero \
                            for s in jointsolutions]) \
                                and len(jointsolutions) > 0:
                        returnfirstsolutions.append(AST.SolverSolution(var.name, \
                                                                       jointeval = jointsolutions, \
                                                                       isHinge = self.IsHinge(var.name)))
                        hasdividebyzero = any([len(self.checkForDivideByZero(self._SubstituteGlobalSymbols(s))) > 0 \
                                               for s in jointsolutions])
                        if not hasdividebyzero:
                            return returnfirstsolutions

                except NotImplementedError, e:
                    # when solve cannot solve an equation
                    log.warn(e)
                    
        if len(returnfirstsolutions) > 0:
            # already computed some solutions, so return them
            # note that this means that all solutions have a divide-by-zero condition
            return returnfirstsolutions
        
        solutions = []
        if len(eqns) > 1:
            neweqns = []
            listsymbols = []
            symbolgen = cse_main.numbered_symbols('const')
            for e in eqns:
                enew, symbols = self.groupTerms(e.subs(varsym.subs), \
                                                [varsym.cvar, varsym.svar, var], \
                                                symbolgen)
                try:
                    # remove coupled equations
                    if any([(m[0]>0) + (m[1]>0) + (m[2]>0) > 1 \
                            for m in Poly(enew, varsym.cvar, varsym.svar, var).monoms()]):
                        continue
                except PolynomialError:
                    continue
                
                try:
                    # ignore any equations with degree 3 or more
                    if max(Poly(enew, varsym.svar).degree_list()) > maxdegree or \
                       max(Poly(enew, varsym.cvar).degree_list()) > maxdegree:
                        log.debug('ignoring equation: ', enew)
                        continue
                except PolynomialError:
                    continue
                
                try:
                    if Poly(enew,varsym.svar).TC() == S.Zero or \
                       Poly(enew,varsym.cvar)      == S.Zero or \
                       Poly(enew,varsym.var)       == S.Zero:
                        log.debug('%s allows trivial solution for %s, ignore', e, varsym.name)
                        continue
                except PolynomialError:
                    continue
                
                rank = self.codeComplexity(enew)
                for s in symbols:
                    rank += self.codeComplexity(s[1])
                neweqns.append((rank, enew))
                listsymbols += symbols
                
            # We only need two equations for two variables, so we sort all equations and
            # start with the least complicated ones until we find a solution
            eqcombinations = []
            for eqs in combinations(neweqns,2):
                eqcombinations.append((eqs[0][0] + eqs[1][0], [Eq(e[1], 0) for e in eqs]))
            eqcombinations.sort(lambda x, y: x[0]-y[0])
            hasgoodsolution = False
            for icomb,comb in enumerate(eqcombinations):
                # skip if too complex
                if len(solutions) > 0 and comb[0] > 200:
                    break
                # try to solve for both sin and cos terms
                if not (self.has(comb[1], varsym.svar) and \
                        self.has(comb[1], varsym.cvar)):
                    continue
                
                try:
                    s = solve(comb[1], [varsym.svar, varsym.cvar])
                except (PolynomialError, CoercionFailed), e:
                    log.debug('solveSingleVariable: failed: %s', e)
                    continue
                
                if s is not None:
                    sollist = [(s[varsym.svar], s[varsym.cvar])] if \
                              s.has_key(varsym.svar) and \
                              s.has_key(varsym.cvar) else [] if \
                              hasattr(s, 'has_key') else s
                    
                    # sollist = None
                    # if hasattr(s, 'has_key'):
                    #     if s.has_key(varsym.svar) and \
                    #        s.has_key(varsym.cvar):
                    #         sollist = [(s[varsym.svar], s[varsym.cvar])]
                    #     else:
                    #         sollist = []
                    # else:
                    #     sollist = s
                        
                    solversolution = AST.SolverSolution(var.name,jointeval = [], \
                                                        isHinge = self.IsHinge(var.name))
                    goodsolution = 0
                    for svarsol, cvarsol in sollist:
                        # solutions cannot be trivial
                        soldiff = (svarsol-cvarsol).subs(listsymbols)
                        soldiffComplexity = self.codeComplexity(soldiff)
                        if soldiffComplexity < 1000 and soldiff.expand() == S.Zero:
                            break
                        
                        svarComplexity = self.codeComplexity(svarsol.subs(listsymbols))
                        cvarComplexity = self.codeComplexity(cvarsol.subs(listsymbols))
                        
                        if  svarComplexity < 600 and \
                            svarsol.subs(listsymbols).expand() == S.Zero and \
                            cvarComplexity < 600 and \
                            Abs(cvarsol.subs(listsymbols).expand()) != S.One:
                            # TGN: this used to be ... - S.One != S.Zero
                            break
                        
                        if cvarComplexity < 600 and \
                           cvarsol.subs(listsymbols).expand() == S.Zero and \
                           svarComplexity < 600 and \
                           Abs(svarsol.subs(listsymbols).expand()) != S.One:
                            # TGN: this used to be ... - S.One != S.Zero
                            break
                        
                        # check the numerator and denominator if solutions are the same or for possible divide by zeros
                        svarfrac = fraction(svarsol)
                        svarfrac = [svarfrac[0].subs(listsymbols), \
                                    svarfrac[1].subs(listsymbols)]
                        cvarfrac = fraction(cvarsol)
                        cvarfrac = [cvarfrac[0].subs(listsymbols), \
                                    cvarfrac[1].subs(listsymbols)]
                        
                        if self.equal(svarfrac[0], cvarfrac[0]) and \
                           self.equal(svarfrac[1], cvarfrac[1]):
                            break
                        
                        if not (\
                                self.isValidSolution(svarfrac[0]) and \
                                self.isValidSolution(svarfrac[1]) and \
                                self.isValidSolution(cvarfrac[0]) and \
                                self.isValidSolution(cvarfrac[1]) ):
                            continue
                        
                        # check if there exists at least one test solution with non-zero denominators
                        if subs is None:
                            testeqs = [svarfrac[1].subs(othersubs), \
                                       cvarfrac[1].subs(othersubs)]
                        else:
                            testeqs = [svarfrac[1].subs(subs).subs(othersubs), \
                                       cvarfrac[1].subs(subs).subs(othersubs)]
                        testsuccess = False
                        
                        for testconsistentvalue in self.testconsistentvalues:
                            if all([testeq.subs(self.globalsymbols).subs(testconsistentvalue).evalf() != S.Zero \
                                    for testeq in testeqs]):
                                testsuccess = True
                                break
                            
                        if not testsuccess:
                            continue
                        scomplexity = self.codeComplexity(svarfrac[0]) + \
                                      self.codeComplexity(svarfrac[1])
                        ccomplexity = self.codeComplexity(cvarfrac[0]) + \
                                      self.codeComplexity(cvarfrac[1])
                        
                        if scomplexity > 1200 or ccomplexity > 1200:
                            log.debug('equation too complex for single variable solution (%d, %d) ' + \
                                      '... (probably wrong?)', scomplexity, ccomplexity)
                            break
                        
                        if scomplexity < 500 and len(str(svarfrac[1])) < 600:
                            # long fractions can take long time to simplify, so we check the length of equation
                            svarfrac[1] = simplify(svarfrac[1])
                            
                        if self.chop(svarfrac[1])== 0:
                            break
                        
                        if ccomplexity < 500 and len(str(cvarfrac[1])) < 600:
                            cvarfrac[1] = simplify(cvarfrac[1])
                            
                        if self.chop(cvarfrac[1])== 0:
                            break
                        # sometimes the returned simplest solution makes really gross approximations

                        # TGN: ensure curvars is a subset of self.trigvars_subs
                        assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                        # equivalent?
                        assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                        
                        svarfracsimp_denom = self.SimplifyTransform(self.trigsimp_new(svarfrac[1]))
                        cvarfracsimp_denom = self.SimplifyTransform(self.trigsimp_new(cvarfrac[1]))
                        # self.SimplifyTransform could help reduce denoms further...
                        denomsequal = False
                        if self.equal(svarfracsimp_denom, cvarfracsimp_denom):
                            denomsequal = True
                        elif self.equal(svarfracsimp_denom, -cvarfracsimp_denom):
                            cvarfrac[0] = -cvarfrac[0]
                            cvarfracsimp_denom = -cvarfracsimp_denom
                            
                        if self.equal(svarfracsimp_denom,cvarfracsimp_denom) and \
                           not svarfracsimp_denom.is_number:
                            log.debug('denom of %s = %s\n' + \
                                      '        do global subs', \
                                      var.name, svarfracsimp_denom)
                            #denom = self.gsymbolgen.next()
                            #solversolution.dictequations.append((denom,sign(svarfracsimp_denom)))

                            # TGN: ensure curvars is a subset of self.trigvars_subs
                            assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                            # equivalent?
                            assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                            
                            svarsolsimp = self.SimplifyTransform(self.trigsimp_new(svarfrac[0]))#*denom)
                            cvarsolsimp = self.SimplifyTransform(self.trigsimp_new(cvarfrac[0]))#*denom)
                            solversolution.FeasibleIsZeros = False
                            solversolution.presetcheckforzeros.append(svarfracsimp_denom)
                            # instead of doing atan2(sign(dummy)*s, sign(dummy)*c)
                            # we do atan2(s,c) + pi/2*(1-1/sign(dummy)) so equations become simpler
                            #
                            # TGN: or just 1-sign(dummy)?
                            #
                            expandedsol = atan2(svarsolsimp,cvarsolsimp) + pi/2*(-S.One + sign(svarfracsimp_denom))
                        else:
                            
                            # TGN: ensure curvars is a subset of self.trigvars_subs
                            assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                            # equivalent?
                            assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                            
                            svarfracsimp_num = self.SimplifyTransform(self.trigsimp_new(svarfrac[0]))
                            cvarfracsimp_num = self.SimplifyTransform(self.trigsimp_new(cvarfrac[0]))
                            svarsolsimp = svarfracsimp_num/svarfracsimp_denom
                            cvarsolsimp = cvarfracsimp_num/cvarfracsimp_denom
                            
                            if svarsolsimp.is_number and cvarsolsimp.is_number:
                                if Abs(svarsolsimp**2+cvarsolsimp**2-S.One).evalf() > 1e-10:
                                    log.debug('%s solution: atan2(%s, %s), sin/cos not on circle; ignore', \
                                              var.name, svarsolsimp, cvarsolsimp)
                                    continue
                                
                            svarsolsimpcomplexity = self.codeComplexity(svarsolsimp)
                            cvarsolsimpcomplexity = self.codeComplexity(cvarsolsimp)
                            if svarsolsimpcomplexity > 3000 or cvarsolsimpcomplexity > 3000:
                                log.warn('new substituted solutions too complex: %d, %d', \
                                         svarsolsimpcomplexity, \
                                         cvarsolsimpcomplexity)
                                continue
                            
                            try:
                                expandedsol = atan2check(svarsolsimp, cvarsolsimp)
                            except RuntimeError, e:
                                log.warn(u'most likely got recursion error when calling atan2: %s', e)
                                continue
                            
                            solversolution.FeasibleIsZeros = False
                            log.debug('solution for %s: atan2 check for joint', var.name)
                        solversolution.jointeval.append(expandedsol)
                        
                        if unknownvars is not None:
                            unsolvedsymbols = []
                            for unknownvar in unknownvars:
                                if unknownvar != var:
                                    unsolvedsymbols += self.Variable(unknownvar).vars
                            if len(unsolvedsymbols) > 0:
                                solversolution.equationsused = [eq for eq in eqns \
                                                                if not eq.has(*unsolvedsymbols)]
                            else:
                                solversolution.equationsused = eqns
                                
                            if len(solversolution.equationsused) > 0:
                                log.info('%s = atan2( %s,\n' + \
                                         '                  %s%s )', \
                                         var.name, \
                                         str(solversolution.equationsused[0]),
                                         ' '*len(var.name), 
                                         str(solversolution.equationsused[1]) )
                                
                        if len(self.checkForDivideByZero(expandedsol.subs(solversolution.dictequations))) == 0:
                            goodsolution += 1
                            
                    if len(solversolution.jointeval) == len(sollist) and len(sollist) > 0:
                        solutions.append(solversolution)
                        if goodsolution > 0:
                            hasgoodsolution = True
                        if len(sollist) == goodsolution and goodsolution == 1 and len(solutions) >= 2:
                            break
                        if len(solutions) >= maxsolutions:
                            # probably more than enough already?
                            break

            if len(solutions) > 0 or hasgoodsolution:
                # found a solution without any divides, necessary for pr2 head_torso lookat3d ik
                return solutions

        # solve one equation
        for ieq, eq in enumerate(eqns):
            symbolgen = cse_main.numbered_symbols('const')
            eqnew, symbols = self.groupTerms(eq.subs(varsym.subs), \
                                             [varsym.cvar, varsym.svar, varsym.var], \
                                             symbolgen)
            try:
                # ignore any equations with degree 3 or more 
                ps = Poly(eqnew, varsym.svar)
                pc = Poly(eqnew, varsym.cvar)
                if max(ps.degree_list()) > maxdegree or \
                   max(pc.degree_list()) > maxdegree:
                    log.debug('cannot solve equation with high degree: %s', str(eqnew))
                    continue
                
                if ps.TC() == S.Zero and len(ps.monoms()) > 0:
                    log.debug('%s has trivial solution, ignore', ps)
                    continue
                
                if pc.TC() == S.Zero and len(pc.monoms()) > 0:
                    log.debug('%s has trivial solution, ignore', pc)
                    continue
                
            except PolynomialError:
                # might not be a polynomial, so ignore
                continue

            equationsused = None
            if unknownvars is not None:
                unsolvedsymbols = []
                for unknownvar in unknownvars:
                    if unknownvar != var:
                        unsolvedsymbols += self.Variable(unknownvar).vars
                if len(unsolvedsymbols) > 0:
                    equationsused = [eq2 for ieq2, eq2 in enumerate(eqns) \
                                     if ieq2 != ieq and not eq2.has(*unsolvedsymbols)]
                else:
                    equationsused = eqns[:]
                    equationsused.pop(ieq)

            numcvar = self.countVariables(eqnew, varsym.cvar)
            numsvar = self.countVariables(eqnew, varsym.svar)
            if numcvar == 1 and numsvar == 1:
                a = Wild('a', exclude = [varsym.svar, varsym.cvar])
                b = Wild('b', exclude = [varsym.svar, varsym.cvar])
                c = Wild('c', exclude = [varsym.svar, varsym.cvar])
                m = eqnew.match(a*varsym.cvar + b*varsym.svar + c)
                if m is not None:
                    symbols += [(varsym.svar, sin(var)), \
                                (varsym.cvar, cos(var))]
                    asinsol = trigsimp(asin(-m[c]/Abs(sqrt(m[a]*m[a]+m[b]*m[b]))).subs(symbols), \
                                       deep = True)
                    # can't use atan2().evalf()... maybe only when m[a] or m[b] is complex?
                    if m[a].has(I) or m[b].has(I):
                        continue
                    constsol = (-atan2(m[a], m[b]).subs(symbols)).evalf()
                    jointsolutions = [constsol + asinsol, \
                                      constsol + pi.evalf() - asinsol]
                    
                    if not constsol.has(I) and \
                       all([self.isValidSolution(s) for s in jointsolutions]) and \
                       len(jointsolutions) > 0:
                        #self.checkForDivideByZero(expandedsol)
                        solutions.append(AST.SolverSolution(var.name, \
                                                            jointeval = jointsolutions, \
                                                            isHinge = self.IsHinge(var.name)))
                        solutions[-1].equationsused = equationsused
                    continue
            if numcvar > 0:
                try:
                    # substitute cos

                    # TGN: the following condition seems weird to me
                    # if  self.countVariables(eqnew, varsym.svar) <= 1 or \
                    #    (self.countVariables(eqnew, varsym.cvar) <= 2 and \
                    #     self.countVariables(eqnew, varsym.svar) == 0):

                    if self.countVariables(eqnew, varsym.svar) <= 1:
                        # anything more than 1 implies quartic equation
                        tempsolutions = solve(eqnew.subs(varsym.svar, sqrt(1-varsym.cvar**2)).expand(), \
                                              varsym.cvar)
                        jointsolutions = []
                        
                        for s in tempsolutions:
                            # TGN: ensure curvars is a subset of self.trigvars_subs
                            assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                            # equivalent?
                            assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                            
                            s2 = self.trigsimp_new(s.subs(symbols+varsym.subsinv))
                            if self.isValidSolution(s2):
                                jointsolutions.append(self.SimplifyTransform(s2))
                        if len(jointsolutions) > 0 and \
                           all([self.isValidSolution(s) \
                                and self.isValidSolution(s) \
                                for s in jointsolutions]):
                            solutions.append(AST.SolverSolution(var.name, \
                                                                jointevalcos = jointsolutions, \
                                                                isHinge = self.IsHinge(var.name)))
                            solutions[-1].equationsused = equationsused
                        continue
                except self.CannotSolveError, e:
                    log.debug(e)
                except NotImplementedError, e:
                    # when solve cannot solve an equation
                    log.warn(e)
            if numsvar > 0:
                # substitute sin
                try:
                    # TGN: the following condition seems weird to me
                    # if  self.countVariables(eqnew, varsym.svar) <= 1 or \
                    #    (self.countVariables(eqnew, varsym.svar) <= 2 and \
                    #     self.countVariables(eqnew, varsym.cvar) == 0):
                    if  self.countVariables(eqnew, varsym.svar) <= 1 or \
                       (self.countVariables(eqnew, varsym.svar) == 2 and \
                        self.countVariables(eqnew, varsym.cvar) == 0):
                        # anything more than 1 implies quartic equation
                        tempsolutions = solve(eqnew.subs(varsym.cvar, \
                                                         sqrt(1-varsym.svar**2)).expand(), \
                                              varsym.svar)

                        # TGN: ensure curvars is a subset of self.trigvars_subs
                        assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                        # equivalent?
                        assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                        
                        jointsolutions = [self.SimplifyTransform(self.trigsimp_new(s.subs(symbols+varsym.subsinv), \
                                                                               )) \
                                          for s in tempsolutions]
                        
                        if all([self.isValidSolution(s) for s in jointsolutions]) and \
                           len(jointsolutions) > 0:
                            solutions.append(AST.SolverSolution(var.name,
                                                                jointevalsin = jointsolutions, \
                                                                isHinge = self.IsHinge(var.name)))
                            solutions[-1].equationsused = equationsused
                        continue
                    
                except self.CannotSolveError, e:
                    log.debug(e)
                    
                except NotImplementedError, e:
                    # when solve cannot solve an equation
                    log.warn(e)

            if numcvar == 0 and numsvar == 0:
                try:
                    tempsolutions = solve(eqnew, var)
                    jointsolutions = []
                    for s in tempsolutions:
                        eqsub = s.subs(symbols)
                        if self.codeComplexity(eqsub) < 2000:
                            
                            # TGN: ensure curvars is a subset of self.trigvars_subs
                            assert(len([z for z in othersolvedvars if z in self.trigvars_subs]) == len(othersolvedvars))
                            # equivalent?
                            assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars]))
                            
                            eqsub = self.SimplifyTransform(self.trigsimp_new(eqsub))
                        jointsolutions.append(eqsub)
                        
                    if all([self.isValidSolution(s) and s != S.Zero \
                            for s in jointsolutions]) and \
                                len(jointsolutions) > 0:
                        solutions.append(AST.SolverSolution(var.name, \
                                                            jointeval = jointsolutions, \
                                                            isHinge = self.IsHinge(var.name)))
                        solutions[-1].equationsused = equationsused
                        
                except NotImplementedError, e:
                    # when solve cannot solve an equation
                    log.warn(e)
                continue
            
            try:
                solution = self.solveHighDegreeEquationsHalfAngle([eqnew], varsym, symbols)
                solutions.append(solution.subs(symbols))
                solutions[-1].equationsused = equationsused
            except self.CannotSolveError, e:
                log.debug(e)
                
        if len(solutions) > 0:                
            return solutions
        
        return [self.solveHighDegreeEquationsHalfAngle(eqns, varsym)]

    def SolvePrismaticHingePairVariables(self, raweqns, var0,var1,othersolvedvars,unknownvars=None):
        """solves one hinge and one prismatic variable together
        """
        if self.IsPrismatic(var0.name) and self.IsHinge(var1.name):
            prismaticSymbol = var0
            hingeSymbol = var1
        elif self.IsHinge(var0.name) and self.IsPrismatic(var1.name):
            hingeSymbol = var0
            prismaticSymbol = var1
        else:
            raise self.CannotSolveError('need to have one hinge and one prismatic variable')
        
        prismaticVariable = self.Variable(prismaticSymbol)
        hingeVariable = self.Variable(hingeSymbol)
        chingeSymbol,shingeSymbol = hingeVariable.cvar, hingeVariable.svar
        varsubs=prismaticVariable.subs+hingeVariable.subs
        varsubsinv = prismaticVariable.subsinv+hingeVariable.subsinv
        unknownvars=[chingeSymbol,shingeSymbol,prismaticSymbol]
        reducesubs = [(shingeSymbol**2,1-chingeSymbol**2)]
        polyeqs = [Poly(eq.subs(varsubs).subs(reducesubs).expand(),unknownvars) for eq in raweqns if eq.has(prismaticSymbol,hingeSymbol)]
        if len(polyeqs) <= 1:
            raise self.CannotSolveError('not enough equations')
        
        # try to solve one variable in terms of the others
        solvevariables = []
        for polyeq in polyeqs:
            if polyeq.degree(0) == 1 and polyeq.degree(1) == 0:
                chingeSolutions = solve(polyeq,chingeSymbol)
                solvevariables.append((prismaticSymbol,[(chingeSymbol,chingeSolutions[0])]))
            elif polyeq.degree(0) == 0 and polyeq.degree(1) == 1:
                shingeSolutions = solve(polyeq,shingeSymbol)
                solvevariables.append((prismaticSymbol,[(shingeSymbol,shingeSolutions[0])]))
            elif polyeq.degree(2) == 1:
                prismaticSolutions = solve(polyeq,prismaticSymbol)
                solvevariables.append((hingeSymbol,[(prismaticSymbol,prismaticSolutions[0])]))
        
        # prioritize solving the hingeSymbol out
        for solveSymbol in [hingeSymbol,prismaticSymbol]:
            for solveSymbol2, solvesubs in solvevariables:
                if solveSymbol == solveSymbol2:
                    # have a solution for one variable, so substitute it in and see if the equations become solvable with one variable
                    reducedeqs = []
                    for polyeq2 in polyeqs:
                        eqnew = simplify(polyeq2.as_expr().subs(solvesubs))
                        if eqnew != S.Zero:
                            reducedeqs.append(eqnew)
                    self.sortComplexity(reducedeqs)
                    try:
                        rawsolutions = self.solveSingleVariable(reducedeqs,solveSymbol,othersolvedvars, unknownvars=unknownvars)
                        if len(rawsolutions) > 0:
                            return rawsolutions

                    except self.CannotSolveError:
                        pass
                
        raise self.CannotSolveError(u'SolvePrismaticHingePairVariables: failed to find variable with degree 1')
        
    def SolvePairVariables(self,raweqns,var0,var1,othersolvedvars,maxcomplexity=50,unknownvars=None):
        """solves two hinge variables together
        """
        # make sure both variables are hinges
        if not self.IsHinge(var0.name) or not self.IsHinge(var1.name):
            raise self.CannotSolveError('pairwise variables only supports hinge joints')
        
        varsym0 = self.Variable(var0)
        varsym1 = self.Variable(var1)
        cvar0,svar0 = varsym0.cvar, varsym0.svar
        cvar1,svar1 = varsym1.cvar, varsym1.svar
        varsubs=varsym0.subs+varsym1.subs
        varsubsinv = varsym0.subsinv+varsym1.subsinv
        unknownvars=[cvar0,svar0,cvar1,svar1]
        reducesubs = [(svar0**2,1-cvar0**2),(svar1**2,1-cvar1**2)]
        eqns = [eq.subs(varsubs).subs(reducesubs).expand() for eq in raweqns if eq.has(var0,var1)]
        if len(eqns) <= 1:
            raise self.CannotSolveError('not enough equation')
        
        # group equations with single variables
        symbolgen = cse_main.numbered_symbols('const')
        orgeqns = []
        allsymbols = []
        for eq in eqns:
            eqnew, symbols = self.groupTerms(eq, unknownvars, symbolgen)
            allsymbols += symbols
            orgeqns.append([self.codeComplexity(eq),Poly(eqnew,*unknownvars)])
        orgeqns.sort(lambda x, y: x[0]-y[0])
        neweqns = orgeqns[:]
        
        pairwisesubs = [(svar0*cvar1,Symbol('s0c1')),(svar0*svar1,Symbol('s0s1')),(cvar0*cvar1,Symbol('c0c1')),(cvar0*svar1,Symbol('c0s1')),(cvar0*svar0,Symbol('s0c0')),(cvar1*svar1,Symbol('c1s1'))]
        pairwiseinvsubs = [(f[1],f[0]) for f in pairwisesubs]
        pairwisevars = [f[1] for f in pairwisesubs]
        reduceeqns = [Poly(eq.as_expr().subs(pairwisesubs),*pairwisevars) for rank,eq in orgeqns if rank < 4*maxcomplexity]
        for i,eq in enumerate(reduceeqns):
            if eq.TC != S.Zero and not eq.TC().is_Symbol:
                n=symbolgen.next()
                allsymbols.append((n,eq.TC().subs(allsymbols)))
                reduceeqns[i] += n-eq.TC()
        
        # try to at least subtract as much paired variables out
        eqcombs = [c for c in combinations(reduceeqns,2)]
        while len(eqcombs) > 0 and len(neweqns) < 20:
            eq0,eq1 = eqcombs.pop()
            eq0dict = eq0.as_dict()
            eq1dict = eq1.as_dict()
            for i in range(6):
                monom = [0,0,0,0,0,0]
                monom[i] = 1
                eq0value = eq0dict.get(tuple(monom),S.Zero)
                eq1value = eq1dict.get(tuple(monom),S.Zero)
                if eq0value != 0 and eq1value != 0:
                    tempeq = (eq0.as_expr()*eq1value-eq0value*eq1.as_expr()).subs(allsymbols+pairwiseinvsubs).expand()
                    if self.codeComplexity(tempeq) > 200:
                        continue
                    eq = simplify(tempeq)
                    if eq == S.Zero:
                        continue
                    
                    peq = Poly(eq,*pairwisevars)
                    if max(peq.degree_list()) > 0 and self.codeComplexity(eq) > maxcomplexity:
                        # don't need such complex equations
                        continue
                    
                    if not self.CheckExpressionUnique(eqns,eq):
                        continue
                    
                    if eq.has(*unknownvars): # be a little strict about new candidates
                        eqns.append(eq)
                        eqnew, symbols = self.groupTerms(eq, unknownvars, symbolgen)
                        allsymbols += symbols
                        neweqns.append([self.codeComplexity(eq),Poly(eqnew,*unknownvars)])

        orgeqns = neweqns[:]
        # try to solve for all pairwise variables
        systemofequations = []
        for i in range(len(reduceeqns)):
            if reduceeqns[i].has(pairwisevars[4],pairwisevars[5]):
                continue
            if not all([__builtin__.sum(m) <= 1 for m in reduceeqns[i].monoms()]):
                continue
            arr = [S.Zero]*5
            for m,c in reduceeqns[i].terms():
                if __builtin__.sum(m) == 1:
                    arr[list(m).index(1)] = c
                else:
                    arr[4] = c
            systemofequations.append(arr)

        if len(systemofequations) >= 4:
            singleeqs = None
            for eqs in combinations(systemofequations,4):
                M = zeros((4,4))
                B = zeros((4,1))
                for i,arr in enumerate(eqs):
                    for j in range(4):
                        M[i,j] = arr[j]
                    B[i] = -arr[4]
                det = self.det_bareis(M,*(self.pvars+unknownvars)).subs(allsymbols)
                if det.evalf() != S.Zero:
                    X = M.adjugate()*B
                    singleeqs = []
                    for i in range(4):
                        eq = (pairwisesubs[i][0]*det - X[i]).subs(allsymbols)
                        eqnew, symbols = self.groupTerms(eq, unknownvars, symbolgen)
                        allsymbols += symbols
                        singleeqs.append([self.codeComplexity(eq),Poly(eqnew,*unknownvars)])
                    break
            if singleeqs is not None:
                neweqns += singleeqs
                neweqns.sort(lambda x, y: x[0]-y[0])

        # check if any equations are at least degree 1 (if not, try to compute some)
        for ivar in range(2):
            polyunknown = []
            for rank,eq in orgeqns:
                p = Poly(eq,unknownvars[2*ivar],unknownvars[2*ivar+1])
                if sum(p.degree_list()) == 1 and __builtin__.sum(p.LM()) == 1:
                    polyunknown.append((rank,p))
            if len(polyunknown) > 0:
                break
        if len(polyunknown) == 0:
            addedeqs = eqns[:]
            polyeqs = []
            for ivar in range(2):
                polyunknown = []
                for rank,eq in orgeqns:
                    p = Poly(eq,unknownvars[2*ivar],unknownvars[2*ivar+1])
                    polyunknown.append(Poly(p.subs(unknownvars[2*ivar+1]**2,1-unknownvars[2*ivar]**2),unknownvars[2*ivar],unknownvars[2*ivar+1]))
                if len(polyunknown) >= 2:
                    monomtoremove = [[polyunknown,(2,0)],[polyunknown,(1,1)]]
                    for curiter in range(2):
                        # remove the square
                        polyunknown,monom = monomtoremove[curiter]
                        pbase = [p for p in polyunknown if p.as_dict().get(monom,S.Zero) != S.Zero]
                        if len(pbase) == 0:
                            continue
                        pbase = pbase[0]
                        pbasedict = pbase.as_dict()
                        for i in range(len(polyunknown)):
                            eq = (polyunknown[i]*pbasedict.get(monom,S.Zero)-pbase*polyunknown[i].as_dict().get(monom,S.Zero)).as_expr().subs(allsymbols)
                            if self.codeComplexity(eq) > 4000:
                                # .. way too complex
                                continue
                            eq = eq.expand()
                            if self.codeComplexity(eq) > 10000:
                                # .. way too complex
                                continue
                            if len(addedeqs) > 10 and self.codeComplexity(eq) > 2000:
                                # .. already have enough...
                                continue
                            if eq != S.Zero and self.CheckExpressionUnique(addedeqs,eq):
                                eqnew, symbols = self.groupTerms(eq, unknownvars, symbolgen)
                                allsymbols += symbols
                                p = Poly(eqnew,*pbase.gens)
                                if p.as_dict().get((1,1),S.Zero) != S.Zero and curiter == 0:
                                    monomtoremove[1][0].insert(0,p)
                                polyeqs.append([self.codeComplexity(eqnew),Poly(eqnew,*unknownvars)])
                                addedeqs.append(eq)
            neweqns += polyeqs
        neweqns.sort(lambda x,y: x[0]-y[0])

        rawsolutions = []
        # try single variable solution, only return if a single solution has been found
        # returning multiple solutions when only one exists can lead to wrong results.
        try:
            rawsolutions += self.solveSingleVariable(self.sortComplexity([e.as_expr().subs(varsubsinv).expand() for score,e in neweqns if not e.has(cvar1,svar1,var1)]),var0,othersolvedvars,subs=allsymbols,unknownvars=unknownvars)
        except self.CannotSolveError:
            pass

        try:
            rawsolutions += self.solveSingleVariable(self.sortComplexity([e.as_expr().subs(varsubsinv).expand() for score,e in neweqns if not e.has(cvar0,svar0,var0)]),var1,othersolvedvars,subs=allsymbols,unknownvars=unknownvars)                    
        except self.CannotSolveError:
            pass

        if len(rawsolutions) > 0:
            solutions = []
            for s in rawsolutions:
                try:
                    solutions.append(s.subs(allsymbols))
                except self.CannotSolveError:
                    pass
                
            if len(solutions) > 0:
                return solutions
        
        groups=[]
        for i,unknownvar in enumerate(unknownvars):
            listeqs = []
            listeqscmp = []
            for rank,eq in neweqns:
                # if variable ever appears, it should be alone
                if all([m[i] == 0 or (__builtin__.sum(m) == m[i] and m[i]>0) for m in eq.monoms()]) and any([m[i] > 0 for m in eq.monoms()]):
                    # make sure there's only one monom that includes other variables
                    othervars = [__builtin__.sum(m) - m[i] > 0 for m in eq.monoms()]
                    if __builtin__.sum(othervars) <= 1:
                        eqcmp = self.removecommonexprs(eq.subs(allsymbols).as_expr(),onlynumbers=False,onlygcd=True)
                        if self.CheckExpressionUnique(listeqscmp,eqcmp):
                            listeqs.append(eq)
                            listeqscmp.append(eqcmp)
            groups.append(listeqs)
        # find a group that has two or more equations:
        useconic=False
        goodgroup = [(i,g) for i,g in enumerate(groups) if len(g) >= 2]
        if len(goodgroup) == 0:
            # might have a set of equations that can be solved with conics
            # look for equations where the variable and its complement are alone
            groups=[]
            for i in [0,2]:
                unknownvar = unknownvars[i]
                complementvar = unknownvars[i+1]
                listeqs = []
                listeqscmp = []
                for rank,eq in neweqns:
                    # if variable ever appears, it should be alone
                    addeq = False
                    if all([__builtin__.sum(m) == m[i]+m[i+1] for m in eq.monoms()]):
                        addeq = True
                    else:
                        # make sure there's only one monom that includes other variables
                        othervars = 0
                        for m in eq.monoms():
                            if __builtin__.sum(m) >  m[i]+m[i+1]:
                                if m[i] == 0 and m[i+1]==0:
                                    othervars += 1
                                else:
                                    othervars = 10000
                        if othervars <= 1:
                            addeq = True
                    if addeq:
                        eqcmp = self.removecommonexprs(eq.subs(allsymbols).as_expr(),onlynumbers=False,onlygcd=True)
                        if self.CheckExpressionUnique(listeqscmp,eqcmp):
                            listeqs.append(eq)
                            listeqscmp.append(eqcmp)
                groups.append(listeqs)
                groups.append([]) # necessary to get indices correct
            goodgroup = [(i,g) for i,g in enumerate(groups) if len(g) >= 2]
            useconic=True
            if len(goodgroup) == 0:
                try:
                    return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)
                except self.CannotSolveError,e:
                    log.warn('%s',e)

                # try a separate approach where the two variables are divided on both sides
                neweqs = []
                for rank,eq in neweqns:
                    p = Poly(eq,unknownvars[0],unknownvars[1])
                    iscoupled = False
                    for m,c in p.terms():
                        if __builtin__.sum(m) > 0:
                            if c.has(unknownvars[2],unknownvars[3]):
                                iscoupled = True
                                break
                    if not iscoupled:
                        neweqs.append([p-p.TC(),Poly(-p.TC(),unknownvars[2],unknownvars[3])])
                if len(neweqs) > 0:
                    for ivar in range(2):
                        lineareqs = [eq for eq in neweqs if __builtin__.sum(eq[ivar].LM())==1]
                        for paireq0,paireq1 in combinations(lineareqs,2):
                            log.info('solving separated equations with linear terms')
                            eq0 = paireq0[ivar]
                            eq0dict = eq0.as_dict()
                            eq1 = paireq1[ivar]
                            eq1dict = eq1.as_dict()
                            disc = (eq0dict.get((1,0),S.Zero)*eq1dict.get((0,1),S.Zero) - eq0dict.get((0,1),S.Zero)*eq1dict.get((1,0),S.Zero)).subs(allsymbols).expand()
                            if disc == S.Zero:
                                continue
                            othereq0 = paireq0[1-ivar].as_expr() - eq0.TC()
                            othereq1 = paireq1[1-ivar].as_expr() - eq1.TC()
                            csol = - eq1dict.get((0,1),S.Zero) * othereq0 + eq0dict.get((0,1),S.Zero) * othereq1
                            ssol = eq1dict.get((1,0),S.Zero) * othereq0 - eq0dict.get((1,0),S.Zero) * othereq1
                            polysymbols = paireq0[1-ivar].gens
                            totaleq = (csol**2+ssol**2-disc**2).subs(allsymbols).expand()
                            if self.codeComplexity(totaleq) < 4000:
                                log.info('simplifying final equation to %d', self.codeComplexity(totaleq))
                                totaleq = simplify(totaleq)
                            ptotal_cos = Poly(Poly(totaleq,*polysymbols).subs(polysymbols[0]**2,1-polysymbols[1]**2).subs(polysymbols[1]**2,1-polysymbols[0]**2),*polysymbols)
                            ptotal_sin = Poly(S.Zero,*polysymbols)
                            for m,c in ptotal_cos.terms():
                                if m[1] > 0:
                                    assert(m[1] == 1)
                                    ptotal_sin = ptotal_sin.sub(Poly.from_dict({(m[0],0):c},*ptotal_sin.gens))
                                    ptotal_cos = ptotal_cos.sub(Poly.from_dict({m:c},*ptotal_cos.gens))

                            ptotalcomplexity = self.codeComplexity(ptotal_cos.as_expr()) + self.codeComplexity(ptotal_sin.as_expr())
                            if ptotalcomplexity < 50000:
                                #log.info('ptotal complexity is %d', ptotalcomplexity)
                                finaleq = (ptotal_cos.as_expr()**2 - (1-polysymbols[0]**2)*ptotal_sin.as_expr()**2).expand()
                                # sometimes denominators can accumulate
                                pfinal = Poly(self.removecommonexprs(finaleq,onlygcd=False,onlynumbers=True),polysymbols[0])
                                pfinal = self.checkFinalEquation(pfinal)
                                if pfinal is not None:
                                    jointsol = atan2(ptotal_cos.as_expr()/ptotal_sin.as_expr(), polysymbols[0])
                                    var = var1 if ivar == 0 else var0
                                    solution = AST.SolverPolynomialRoots(jointname=var.name,poly=pfinal,jointeval=[jointsol],isHinge=self.IsHinge(var.name))
                                    solution.postcheckforzeros = [ptotal_sin.as_expr()]
                                    solution.postcheckfornonzeros = []
                                    solution.postcheckforrange = []
                                    return [solution]
                                
                # if maxnumeqs is any less, it will miss linearly independent equations
                lineareqs = self.solveSingleVariableLinearly(raweqns,var0,[var1],maxnumeqs=len(raweqns))
                if len(lineareqs) > 0:
                    try:
                        return [self.solveHighDegreeEquationsHalfAngle(lineareqs,varsym1)]
                    except self.CannotSolveError,e:
                        log.warn('%s',e)

                raise self.CannotSolveError('cannot cleanly separate pair equations')

        varindex=goodgroup[0][0]
        var = var0 if varindex < 2 else var1
        varsym = varsym0 if varindex < 2 else varsym1
        unknownvar=unknownvars[goodgroup[0][0]]
        eqs = goodgroup[0][1][0:2]
        simpleterms = []
        complexterms = []
        domagicsquare = False
        for i in range(2):
            if useconic:
                terms=[(c,m) for m,c in eqs[i].terms() if __builtin__.sum(m) - m[varindex] - m[varindex+1] > 0]
            else:
                terms=[(c,m) for m,c in eqs[i].terms() if __builtin__.sum(m) - m[varindex] > 0]
            if len(terms) > 0:
                simpleterms.append(eqs[i].sub(Poly.from_dict({terms[0][1]:terms[0][0]},*eqs[i].gens)).as_expr()/terms[0][0]) # divide by the coeff
                complexterms.append(Poly({terms[0][1]:S.One},*unknownvars).as_expr())
                domagicsquare = True
            else:
                simpleterms.append(eqs[i].as_expr())
                complexterms.append(S.Zero)
        finaleq = None
        checkforzeros = []
        if domagicsquare:

            # TGN: ensure curvars is a subset of self.trigvars_subs
            assert(len([z for z in othersolvedvars+[var0,var1] if z in self.trigvars_subs]) == len(othersolvedvars+[var0,var1]))
            # equivalent?
            assert(not any([(z not in self.trigvars_subs) for z in othersolvedvars+[var0,var1]]))

            # here is the magic transformation:
            finaleq = self.trigsimp_new(expand(((complexterms[0]**2+complexterms[1]**2) \
                                                - simpleterms[0]**2 - simpleterms[1]**2).subs(varsubsinv))).subs(varsubs)
            
            denoms = [fraction(simpleterms[0])[1], \
                      fraction(simpleterms[1])[1], \
                      fraction(complexterms[0])[1], \
                      fraction(complexterms[1])[1]]
            
            lcmvars = self.pvars+unknownvars
            for othersolvedvar in othersolvedvars:
                lcmvars += self.Variable(othersolvedvar).vars
            denomlcm = Poly(S.One,*lcmvars)
            for denom in denoms:
                if denom != S.One:
                    checkforzeros.append(self.removecommonexprs(denom,onlygcd=False,onlynumbers=True))
                    denomlcm = Poly(lcm(denomlcm,denom),*lcmvars)
            finaleq = simplify(finaleq*denomlcm.as_expr()**2)
            complementvarindex = varindex-(varindex%2)+((varindex+1)%2)
            complementvar = unknownvars[complementvarindex]
            finaleq = simplify(finaleq.subs(complementvar**2,1-unknownvar**2)).subs(allsymbols).expand()
        else:
            # try to reduce finaleq
            p0 = Poly(simpleterms[0],unknownvars[varindex],unknownvars[varindex+1])
            p1 = Poly(simpleterms[1],unknownvars[varindex],unknownvars[varindex+1])
            if max(p0.degree_list()) > 1 \
               and max(p1.degree_list()) > 1 \
               and max(p0.degree_list()) == max(p1.degree_list()) \
               and p0.LM() == p1.LM():
                finaleq = (p0*p1.LC()-p1*p0.LC()).as_expr()
                finaleq = expand(simplify(finaleq.subs(allsymbols)))
                if finaleq == S.Zero:
                    finaleq = expand(p0.as_expr().subs(allsymbols))
        if finaleq is None:
            log.warn('SolvePairVariables: did not compute a final variable. This is a weird condition...')
            return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)
        
        if not self.isValidSolution(finaleq):
            log.warn('failed to solve pairwise equation: %s'%str(finaleq))
            return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)

        newunknownvars = unknownvars[:]
        newunknownvars.remove(unknownvar)
        if finaleq.has(*newunknownvars):
            log.warn('equation relies on unsolved variables(%s):\n' + \
                     '        %s',newunknownvars, finaleq)
            return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)

        if not finaleq.has(unknownvar):
            # somehow removed all variables, so try the general method
            return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)

        try:
            if self.codeComplexity(finaleq) > 100000:
                return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)
            
        except self.CannotSolveError:
            pass

        if useconic:
            # conic roots solver not as robust as half-angle transform!
            #return [SolverConicRoots(var.name,[finaleq],isHinge=self.IsHinge(var.name))]
            solution = self.solveHighDegreeEquationsHalfAngle([finaleq],varsym)
            solution.checkforzeros += checkforzeros
            return [solution]

        # now that everything is with respect to one variable, simplify and solve the equation
        eqnew, symbols = self.groupTerms(finaleq, unknownvars, symbolgen)
        allsymbols += symbols
        solutions = solve(eqnew,unknownvar)
        log.info('pair solution: %s, %s', eqnew,solutions)
        if solutions:
            solversolution = AST.SolverSolution(var.name, isHinge=self.IsHinge(var.name))
            processedsolutions = []
            for s in solutions:
                processedsolution = s.subs(allsymbols+varsubsinv).subs(varsubs)
                # trigsimp probably won't work on long solutions
                if self.codeComplexity(processedsolution) < 2000: # complexity of 2032 for pi robot freezes
                    log.info('solution complexity: %d', self.codeComplexity(processedsolution))
                    processedsolution = self.SimplifyTransform(self.trigsimp(processedsolution,othersolvedvars))
                processedsolutions.append(processedsolution.subs(varsubs))
            if (varindex%2)==0:
                solversolution.jointevalcos=processedsolutions
            else:
                solversolution.jointevalsin=processedsolutions
            return [solversolution]
        
        return self.SolvePairVariablesHalfAngle(raweqns,var0,var1,othersolvedvars)
        #raise self.CannotSolveError('cannot solve pair equation')
        
    ## SymPy helper routines

    @staticmethod
    def isValidSolution(expr):
        """
        Returns True if solution does not contain any I, nan, or oo
        """
        
        if expr.is_number:
            e = expr.evalf()
            return not (e.has(I) or isinf(e) or isnan(e))
        
        elif expr.is_Mul:

            expr_num    = sum([ num for num in expr.args if num.is_number ]) + Float(0)
            expr_others = [ num for num in expr.args if not num.is_number ]

            return (not (expr_num.has(I) or isinf(expr_num) or isnan(expr_num))) and \
                all([IKFastSolver.isValidSolution(arg) for arg in expr_others])

        else:
            return all([IKFastSolver.isValidSolution(arg) for arg in expr.args])

        assert(0) # TGN: cannot reach here
        return True

    @staticmethod
    def _GetSumSquares(expr):
        """
        if expr is a sum of squares, returns the list of individual expressions that were squared. 
        otherwise returns None
        """
        values = []
        if expr.is_Add:
            for arg in expr.args:
                if arg.is_Pow and arg.exp.is_number and arg.exp > 0 and (arg.exp%2) == 0:
                    values.append(arg.base)
                else:
                    return []
                
        elif expr.is_Mul:
            values = IKFastSolver._GetSumSquares(expr.args[0])
            for arg in expr.args[1:]:
                values2 = IKFastSolver._GetSumSquares(arg)
                if len(values2) > 0:
                    values = [x*y for x,y in product(values,values2)]
                else:
                    values = [x*arg for x in values]
            return values
        
        else:
            if expr.is_Pow and expr.exp.is_number and expr.exp > 0 and (expr.exp%2) == 0:
                values.append(expr.base)
            
        return values
    
    @staticmethod
    def recursiveFraction(expr):
        """
        return the numerator and denominator of the expression as if it was one fraction
        """
        if expr.is_Add:
            allpoly = []
            finaldenom = S.One
            for arg in expr.args:
                n,d = IKFastSolver.recursiveFraction(arg)
                finaldenom = finaldenom*d
                allpoly.append([n,d])
            finalnum = S.Zero
            for n,d in allpoly:
                finalnum += n*(finaldenom/d)
            return finalnum,finaldenom
        elif expr.is_Mul:
            finalnum = S.One
            finaldenom = S.One
            for arg in expr.args:
                n,d = IKFastSolver.recursiveFraction(arg)
                finalnum = finalnum * n
                finaldenom = finaldenom * d
            return finalnum,finaldenom
        elif expr.is_Pow and expr.exp.is_number:
            n,d=IKFastSolver.recursiveFraction(expr.base)
            if expr.exp < 0:
                exponent = -expr.exp
                n,d = d,n
            else:
                exponent = expr.exp
            return n**exponent,d**exponent
        else:
            return fraction(expr)

    @staticmethod
    def groupTerms(expr, vars, symbolgen = None):
        """
        Separates all terms that do have var in them
        """
        
        if symbolgen is None:
            symbolgen = cse_main.numbered_symbols('const')
            
        symbols = []
        try:
            p = Poly(expr, *vars)
        except PolynomialError:
            return expr, symbols
        
        newexpr = S.Zero
        for m, c in p.terms():
            # make huge numbers into constants too
            if (c.is_number and len(str(c)) > 40) or \
               not (c.is_number or c.is_Symbol):
                # if it is a product of a symbol and a number, then ignore
                if not (c.is_Mul and all([e.is_number or e.is_Symbol for e in c.args])):
                    sym = symbolgen.next()
                    symbols.append((sym,c))
                    c = sym
            if __builtin__.sum(m) == 0:
                newexpr += c
            else:
                for i,degree in enumerate(m):
                    c = c*vars[i]**degree
                newexpr += c
        return newexpr, symbols

    @staticmethod
    def replaceNumbers(expr, symbolgen = None):
        """Replaces all numbers with symbols, this is to make gcd faster when fractions get too big"""
        if symbolgen is None:
            symbolgen = cse_main.numbered_symbols('const')
        symbols = []
        if expr.is_number:
            result = symbolgen.next()
            symbols.append((result, expr))
        elif expr.is_Mul:
            result = S.One
            for arg in expr.args:
                newresult, newsymbols = IKFastSolver.replaceNumbers(arg, symbolgen)
                result *= newresult
                symbols += newsymbols
        elif expr.is_Add:
            result = S.Zero
            for arg in expr.args:
                newresult, newsymbols = IKFastSolver.replaceNumbers(arg, symbolgen)
                result += newresult
                symbols += newsymbols
        elif expr.is_Pow:
            # don't replace the exponent
            newresult, newsymbols = IKFastSolver.replaceNumbers(expr.base, symbolgen)
            symbols += newsymbols
            result = newresult**expr.exp
        else:
            result = expr
        return result,symbols

    @staticmethod
    def frontnumbers(eq):
        if eq.is_Number:
            return [eq]
        if eq.is_Mul:
            n = []
            for arg in eq.args:
                n += IKFastSolver.frontnumbers(arg)
            return n
        return []

    def IsAnyImaginaryByEval(self, eq):
        """checks if an equation ever evaluates to an imaginary number
        """
        for testconsistentvalue in self.testconsistentvalues:
            value = eq.subs(testconsistentvalue).evalf()
            if value.is_complex and not value.is_real:
                return True
            
        return False

    def AreAllImaginaryByEval(self, eq):
        """checks if an equation ever evaluates to an imaginary number
        """
        for testconsistentvalue in self.testconsistentvalues:
            value = eq.subs(testconsistentvalue).evalf()
            if not (value.is_complex and not value.is_real):
                return False
            
        return True
    
    def IsDeterminantNonZeroByEval(self, A, evalfirst=True):
        """checks if a determinant is non-zero by evaluating all the possible solutions.
        :param evalfirst: if True, then call evalf() first before any complicated operation in order to avoid freezes. Set this to false to get more accurate results when A is known to be simple.
        :return: True if there exist values where det(A) is not zero
        """
        N = A.shape[0]
        thresh = 0.0003**N # when translationdirection5d is used with direction that is 6+ digits, the determinent gets small... pi_robot requires 0.0003**N
        if evalfirst:
            if thresh > 1e-14:
                # make sure thresh isn't too big...
                thresh = 1e-14
        else:
            # can have a tighter thresh since evaluating last...
            if thresh > 1e-40:
                # make sure thresh isn't too big...
                thresh = 1e-40
                
        nummatrixsymbols = __builtin__.sum([1 for a in A if not a.is_number])
        if nummatrixsymbols == 0:
            if evalfirst:
                return abs(A.evalf().det()) > thresh
            else:
                return abs(A.det().evalf()) > thresh
        
        for testconsistentvalue in self.testconsistentvalues:
            if evalfirst:
                detvalue = A.subs(testconsistentvalue).evalf().det()
            else:
                detvalue = A.subs(testconsistentvalue).det().evalf()
            if abs(detvalue) > thresh:
                return True
            
        return False
    
    @staticmethod
    def removecommonexprs(eq, \
                          returncommon = False, \
                          onlygcd = False, \
                          onlynumbers = True):
        """
        Factors out common expressions from a sum, assuming all coefficients are rational.
 
        E.g. from a*c_0 + a*c_1 + a*c_2 = 0 we obtain c_0 + c_1 + c_2 = 0
        """
        eq = eq.expand() # doesn't work otherwise

        # use with "from operator import mul"
        assert(reduce(mul,[],1) == S.One)
        assert(sum([]) == S.Zero)
        
        if eq.is_Add:
            exprs = eq.args
            totaldenom = S.One
            common = S.One
            len_exprs = len(exprs)
            if onlynumbers:
                for i in range(len_exprs):
                    denom = reduce(mul, IKFastSolver.frontnumbers(fraction(exprs[i])[1]), 1)
                    if denom != S.One:
                        exprs = [expr*denom for expr in exprs]
                        totaldenom *= denom
                if onlygcd:
                    common = None
                    for i in range(len_exprs):
                        coeff = reduce(mul, IKFastSolver.frontnumbers(exprs[i]), 1)
                        if common == None:
                            common = coeff
                        else:
                            common = igcd(common,coeff)
                        if common == S.One:
                            break
            else:
                for i in range(len_exprs):
                    denom = fraction(exprs[i])[1]
                    if denom != S.One:
                        exprs = [expr*denom for expr in exprs]
                        totaldenom *= denom
                        
                # there are no fractions, so we can start simplifying
                common = exprs[0]/fraction(cancel(exprs[0]/exprs[1]))[0]
                for i in range(2, len_exprs):
                    common = common/fraction(cancel(common/exprs[i]))[0]
                    if common.is_number:
                        common = S.One
                        
            # find the smallest number and divide by it
            if not onlygcd:
                smallestnumber = None
                for expr in exprs:
                    if expr.is_number \
                       and (smallestnumber is None or smallestnumber > Abs(expr)):
                            smallestnumber = Abs(expr)

                    elif expr.is_Mul:
                        n = reduce(mul, [arg for arg in expr.args if arg.is_number], 1)
                        if smallestnumber is None or smallestnumber > Abs(n):
                            smallestnumber = Abs(n)
                            
                if smallestnumber is not None:
                    common = common*smallestnumber
                    
            eq = sum(expr/common for expr in exprs)
            if returncommon:
                return eq, common/totaldenom
            
        elif eq.is_Mul:
            coeff = reduce(mul, IKFastSolver.frontnumbers(eq), 1)

            if returncommon:
                return eq/coeff, coeff
            return eq/coeff
        
        if returncommon:
            return eq, S.One
        
        return eq

    @staticmethod
    def det_bareis(M, *vars, **kwargs):
        """Function from sympy with a couple of improvements.
           Compute matrix determinant using Bareis' fraction-free
           algorithm which is an extension of the well known Gaussian
           elimination method. This approach is best suited for dense
           symbolic matrices and will result in a determinant with
           minimal number of fractions. It means that less term
           rewriting is needed on resulting formulae.

           TODO: Implement algorithm for sparse matrices (SFF).

           Function from sympy/matrices/matrices.py
        """
        if not M.is_square:
            raise NonSquareMatrixException()
        
        n = M.rows
        M = M[:,:] # make a copy
        if n == 1:
            det = M[0, 0]
        elif n == 2:
            det = M[0, 0]*M[1, 1] - M[0, 1]*M[1, 0]
        else:
            sign = 1 # track current sign in case of column swap

            for k in range(n-1):
                # look for a pivot in the current column
                # and assume det == 0 if none is found
                if M[k, k] == 0:
                    for i in range(k+1, n):
                        if M[i, k] != 0:
                            M.row_swap(i, k)
                            sign *= -1
                            break
                    else:
                        return S.Zero

                # proceed with Bareis' fraction-free (FF)
                # form of Gaussian elimination algorithm
                for i in range(k+1, n):
                    for j in range(k+1, n):
                        D = M[k, k]*M[i, j] - M[i, k]*M[k, j]

                        if k > 0:
                            if len(vars) > 0 and D != S.Zero and not M[k-1, k-1].is_number:
                                try:
                                    D,r = div(Poly(D,*vars),M[k-1, k-1])
                                except UnificationFailed:
                                    log.warn('unification failed, trying direct division')
                                    D /= M[k-1, k-1]
                            else:
                                D /= M[k-1, k-1]

                        if D.is_Atom:
                            M[i, j] = D
                        else:
                            if len(vars) > 0:
                                M[i, j] = D
                            else:
                                M[i, j] = Poly.cancel(D)

            det = sign * M[n-1, n-1]
            
        return det.expand()

    @staticmethod
    def LUdecompositionFF(self,*vars):
        """
        Compute a fraction-free LU decomposition.

        Returns 4 matrices P, L, D, U such that PA = L D**-1 U.
        If the elements of the matrix belong to some integral domain I, then all
        elements of L, D and U are guaranteed to belong to I.

        **Reference**
            - W. Zhou & D.J. Jeffrey, "Fraction-free matrix factors: new forms
              for LU and QR factors". Frontiers in Computer Science in China,
              Vol 2, no. 1, pp. 67-80, 2008.
        """
        n, m = self.rows, self.cols
        U, L, P = self[:,:], eye(n), eye(n)
        DD = zeros(n) # store it smarter since it's just diagonal
        oldpivot = S.One

        for k in range(n-1):
            log.info('row=%d', k)
            if U[k,k] == 0:
                for kpivot in range(k+1, n):
                    if U[kpivot, k] != 0:
                        break
                else:
                    raise ValueError("Matrix is not full rank")
                U[k, k:], U[kpivot, k:] = U[kpivot, k:], U[k, k:]
                L[k, :k], L[kpivot, :k] = L[kpivot, :k], L[k, :k]
                P[k, :], P[kpivot, :] = P[kpivot, :], P[k, :]
            L[k,k] = Ukk = U[k,k]
            DD[k,k] = oldpivot * Ukk
            for i in range(k+1, n):
                L[i,k] = Uik = U[i,k]
                for j in range(k+1, m):
                    #U[i,j] = simplify((Ukk * U[i,j] - U[k,j]*Uik) / oldpivot)
                    D = Ukk * U[i,j] - U[k,j]*Uik
                    if len(vars) > 0 and D != S.Zero and not oldpivot.is_number:
                        try:
                            D,r = div(Poly(D,*vars),oldpivot)
                        except UnificationFailed:
                            log.warn('unification failed, trying direct division')
                            D /= oldpivot
                    else:
                        D /= oldpivot
                    # save
                    if D.is_Atom:
                        U[i,j] = D.as_expr()
                    else:
                        if len(vars) > 0:
                            U[i,j] = D.as_expr()
                        else:
                            U[i,j] = D.cancel()
                U[i,k] = 0
            oldpivot = Ukk
        DD[n-1,n-1] = oldpivot
        return P, L, DD, U

    @staticmethod
    def sequence_cross_product(*sequences):
        """iterates through the cross product of all items in the sequences"""
        # visualize an odometer, with "wheels" displaying "digits"...:
        wheels = map(iter, sequences)
        digits = [it.next( ) for it in wheels]
        while True:
            yield tuple(digits)
            for i in range(len(digits)-1, -1, -1):
                try:
                    digits[i] = wheels[i].next( )
                    break
                except StopIteration:
                    wheels[i] = iter(sequences[i])
                    digits[i] = wheels[i].next( )
            else:
                break

    @staticmethod
    def tolatex(e):
        s = printing.latex(e)
        s1 = re.sub('\\\\operatorname\{(sin|cos)\}\\\\left\(j_\{(\d)\}\\\\right\)','\g<1>_\g<2>',s)
        s2 = re.sub('1\.(0*)([^0-9])','1\g<2>',s1)
        s3 = re.sub('1 \\\\(sin|cos)','\g<1>',s2)
        s4 = re.sub('(\d*)\.([0-9]*[1-9])(0*)([^0-9])','\g<1>.\g<2>\g<4>',s3)
        s5 = re.sub('sj_','s_',s4)
        s5 = re.sub('cj_','c_',s5)
        s5 = re.sub('sin','s',s5)
        s5 = re.sub('cos','c',s5)
        replacements = [('px','p_x'),('py','p_y'),('pz','p_z'),('r00','r_{00}'),('r01','r_{01}'),('r02','r_{02}'),('r10','r_{10}'),('r11','r_{11}'),('r12','r_{12}'),('r20','r_{20}'),('r21','r_{21}'),('r022','r_{22}')]
        for old,new in replacements:
            s5 = re.sub(old,new,s5)
        return s5

    @staticmethod
    def GetSolvers():
        """Returns a dictionary of all the supported solvers and their official identifier names"""
        return {'transform6d'                 :IKFastSolver.solveFullIK_6D,
                'rotation3d'                  :IKFastSolver.solveFullIK_Rotation3D,
                'translation3d'               :IKFastSolver.solveFullIK_Translation3D,
                'direction3d'                 :IKFastSolver.solveFullIK_Direction3D,
                'ray4d'                       :IKFastSolver.solveFullIK_Ray4D,
                'lookat3d'                    :IKFastSolver.solveFullIK_Lookat3D,
                'translationdirection5d'      :IKFastSolver.solveFullIK_TranslationDirection5D,
                'translationxy2d'             :IKFastSolver.solveFullIK_TranslationXY2D,
                'translationxyorientation3d'  :IKFastSolver.solveFullIK_TranslationXYOrientation3D,
                'translationxaxisangle4d'     :IKFastSolver.solveFullIK_TranslationAxisAngle4D,
                'translationyaxisangle4d'     :IKFastSolver.solveFullIK_TranslationAxisAngle4D,
                'translationzaxisangle4d'     :IKFastSolver.solveFullIK_TranslationAxisAngle4D,
                'translationxaxisangleznorm4d':IKFastSolver.solveFullIK_TranslationAxisAngle4D,
                'translationyaxisanglexnorm4d':IKFastSolver.solveFullIK_TranslationAxisAngle4D,
                'translationzaxisangleynorm4d':IKFastSolver.solveFullIK_TranslationAxisAngle4D
                }
