import struct
import logging

import angr
import claripy

from .builder import Builder
from .. import rop_utils
from ..errors import RopException
from ..rop_chain import RopChain

l = logging.getLogger(__name__)

class MemChanger(Builder):
    """
    part of angrop's chainbuilder engine, responsible for adding values to a memory location
    """
    def __init__(self, project, arch, gadgets, reg_setter, badbytes=None, filler=None):
        super().__init__(project, arch, badbytes=badbytes, filler=filler)

        self._reg_setter = reg_setter
        self._mem_change_gadgets = self._get_all_mem_change_gadgets(gadgets)

    def _set_regs(self, *args, **kwargs):
        return self._reg_setter.run(*args, **kwargs)

    @staticmethod
    def _get_all_mem_change_gadgets(gadgets):
        possible_gadgets = set()
        for g in gadgets:
            if len(g.mem_reads) + len(g.mem_writes) > 0 or len(g.mem_changes) != 1:
                continue
            if g.bp_moves_to_sp:
                continue
            if g.stack_change <= 0:
                continue
            for m_access in g.mem_changes:
                if m_access.addr_controllable() and m_access.data_controllable() and m_access.addr_data_independent():
                    possible_gadgets.add(g)
        return possible_gadgets

    def add_to_mem(self, addr, value, data_size=None):
        # assume we need intersection of addr_dependencies and data_dependencies to be 0
        # TODO could allow mem_reads as long as we control the address?

        if data_size is None:
            data_size = self.project.arch.bits

        possible_gadgets = {x for x in self._mem_change_gadgets
                    if x.mem_changes[0].op in ('__add__', '__sub__') and x.mem_changes[0].data_size == data_size}

        # get the data from trying to set all the registers
        registers = dict((reg, 0x41) for reg in self._reg_setter._reg_set)
        l.debug("getting reg data for mem adds")
        _, _, reg_data = self._reg_setter._find_reg_setting_gadgets(max_stack_change=0x50, **registers)
        l.debug("trying mem_add gadgets")

        best_stack_change = 0xffffffff
        best_gadget = None
        for t, vals in reg_data.items():
            if vals[1] >= best_stack_change:
                continue
            for g in possible_gadgets:
                mem_change = g.mem_changes[0]
                if (set(mem_change.addr_dependencies) | set(mem_change.data_dependencies)).issubset(set(t)):
                    stack_change = g.stack_change + vals[1]
                    if stack_change < best_stack_change:
                        best_gadget = g
                        best_stack_change = stack_change

        if best_gadget is None:
            raise RopException("Couldnt set registers for any memory add gadget")

        l.debug("Now building the mem add chain")

        # build the chain
        chain = self._change_mem_with_gadget(best_gadget, addr, data_size, difference=value)
        return chain

    def _change_mem_with_gadget(self, gadget, addr, data_size, final_val=None, difference=None):
        # sanity check for simple gadget
        if len(gadget.mem_writes) + len(gadget.mem_changes) != 1 or len(gadget.mem_reads) != 0:
            raise RopException("too many memory accesses for my lazy implementation")

        if (final_val is not None and difference is not None) or (final_val is None and difference is None):
            raise RopException("must specify difference or final value and not both")

        arch_endness = self.project.arch.memory_endness

        # constrain the successor to be at the gadget
        # emulate 'pop pc'
        test_state = self.make_sim_state(gadget.addr)

        if difference is not None:
            test_state.memory.store(addr, test_state.solver.BVV(~difference, data_size)) # pylint:disable=invalid-unary-operand-type
        if final_val is not None:
            test_state.memory.store(addr, test_state.solver.BVV(~final_val, data_size)) # pylint:disable=invalid-unary-operand-type

        # step the gadget
        pre_gadget_state = test_state
        state = rop_utils.step_to_unconstrained_successor(self.project, pre_gadget_state)

        # constrain the change
        mem_change = gadget.mem_changes[0]
        the_action = None
        for a in state.history.actions.hardcopy:
            if a.type != "mem" or a.action != "write":
                continue
            if set(rop_utils.get_ast_dependency(a.addr.ast)) == set(mem_change.addr_dependencies):
                the_action = a
                break

        if the_action is None:
            raise RopException("Couldn't find the matching action")

        # constrain the addr
        test_state.add_constraints(the_action.addr.ast == addr)
        pre_gadget_state.add_constraints(the_action.addr.ast == addr)
        pre_gadget_state.options.discard(angr.options.AVOID_MULTIVALUED_WRITES)
        pre_gadget_state.options.discard(angr.options.AVOID_MULTIVALUED_READS)
        state = rop_utils.step_to_unconstrained_successor(self.project, pre_gadget_state)

        # constrain the data
        if final_val is not None:
            test_state.add_constraints(state.memory.load(addr, data_size//8, endness=arch_endness) ==
                                       test_state.solver.BVV(final_val, data_size))
        if difference is not None:
            test_state.add_constraints(state.memory.load(addr, data_size//8, endness=arch_endness) -
                                       test_state.memory.load(addr, data_size//8, endness=arch_endness) ==
                                       test_state.solver.BVV(difference, data_size))

        # get the actual register values
        all_deps = list(mem_change.addr_dependencies) + list(mem_change.data_dependencies)
        reg_vals = {}
        for reg in set(all_deps):
            reg_vals[reg] = test_state.solver.eval(test_state.registers.load(reg))

        chain = self._set_regs(**reg_vals)
        chain.add_gadget(gadget)

        bytes_per_pop = self.project.arch.bytes
        for _ in range(gadget.stack_change // bytes_per_pop - 1):
            chain.add_value(self._get_fill_val())
        return chain

    def _get_fill_val(self):
        if self._roparg_filler is not None:
            return self._roparg_filler
        else:
            return claripy.BVS("filler", self.project.arch.bits)