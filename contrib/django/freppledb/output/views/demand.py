#
# Copyright (C) 2007-2012 by Johan De Taeye, frePPLe bvba
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

# file : $URL$
# revision : $LastChangedRevision$  $LastChangedBy$
# date : $LastChangedDate$

from django.db import connections
from django.utils.translation import ugettext_lazy as _
from django.utils.text import capfirst
from django.utils.encoding import force_unicode
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.template import RequestContext, loader
from django.conf import settings

from freppledb.input.models import Item
from freppledb.output.models import Demand
from freppledb.common.db import python_date, sql_datediff, sql_overlap
from freppledb.common.report import getBuckets
from freppledb.common.report import GridReport, GridPivot, GridFieldText, GridFieldNumber, GridFieldDateTime, GridFieldInteger


class OverviewReport(GridPivot):
  '''
  A report showing the independent demand for each item.
  '''
  template = 'output/demand.html'
  title = _('Demand report')
  basequeryset = Item.objects.all()
  model = Item
  rows = (
    GridFieldText('item', title=_('item'), key=True, field_name='name', formatter='item', editable=False),
    GridFieldText(None, width="(5*numbuckets<200 ? 5*numbuckets : 200)", extra='formatter:graph', editable=False),
    )
  crosses = (
    ('demand',{'title': _('demand')}),
    ('supply',{'title': _('total supply')}),
    ('backlog',{'title': _('backlog')}),
    )

  @classmethod 
  def extra_context(reportclass, request, *args, **kwargs):
    if args and args[0]:
      return {
        'title': capfirst(force_unicode(Item.Meta.verbose_name) + " " + args[0]),
        'post_title': ': ' + capfirst(force_unicode(_('plan'))),
        }      
    else:
      return {}

  @staticmethod
  def query(request, basequery, bucket, startdate, enddate, sortsql='1 asc'):
    basesql, baseparams = basequery.query.get_compiler(basequery.db).as_sql(with_col_aliases=True)
    cursor = connections[request.database].cursor()

    # Assure the item hierarchy is up to date
    Item.rebuildHierarchy(database=basequery.db)

    # Execute a query to get the backlog at the start of the horizon
    startbacklogdict = {}
    query = '''
      select items.name, sum(quantity)
      from (%s) items
      inner join item
      on item.lft between items.lft and items.rght
      inner join out_demand
      on item.name = out_demand.item
        and (plandate is null or plandate >= '%s')
        and due < '%s'
      group by items.name
      ''' % (basesql, startdate, startdate)
    cursor.execute(query, baseparams)
    for row in cursor.fetchall():
      if row[0]: startbacklogdict[row[0]] = float(row[1])

    # Execute the query
    query = '''
        select y.name as row1,
               y.bucket as col1, y.startdate as col2, y.enddate as col3,
               min(y.orders),
               min(y.planned)
        from (
          select x.name as name, x.lft as lft, x.rght as rght,
               x.bucket as bucket, x.startdate as startdate, x.enddate as enddate,
               coalesce(sum(demand.quantity),0) as orders,
               min(x.planned) as planned
          from (
          select items.name as name, items.lft as lft, items.rght as rght,
                 d.bucket as bucket, d.startdate as startdate, d.enddate as enddate,
                 coalesce(sum(out_demand.quantity),0) as planned
          from (%s) items
          -- Multiply with buckets
          cross join (
             select name as bucket, startdate, enddate
             from common_bucketdetail
             where bucket_id = '%s' and enddate > '%s' and startdate < '%s'
             ) d
          -- Include hierarchical children
          inner join item
          on item.lft between items.lft and items.rght
          -- Planned quantity
          left join out_demand
          on item.name = out_demand.item
          and d.startdate <= out_demand.plandate
          and d.enddate > out_demand.plandate
          and out_demand.plandate >= '%s'
          and out_demand.plandate < '%s'          
          -- Grouping
          group by items.name, items.lft, items.rght, d.bucket, d.startdate, d.enddate
        ) x
        -- Requested quantity
        inner join item
        on item.lft between x.lft and x.rght
        left join demand
        on item.name = demand.item_id
        and x.startdate <= demand.due
        and x.enddate > demand.due
        and demand.due >= '%s'
        and demand.due < '%s'          
        -- Grouping
        group by x.name, x.lft, x.rght, x.bucket, x.startdate, x.enddate
        ) y
        -- Ordering and grouping
        group by y.name, y.lft, y.rght, y.bucket, y.startdate, y.enddate
        order by %s, y.startdate
       ''' % (basesql,bucket,startdate,enddate,startdate,enddate,startdate,enddate,sortsql)
    cursor.execute(query,baseparams)

    # Build the python result
    previtem = None
    for row in cursor.fetchall():
      if row[0] != previtem:
        try: backlog =  startbacklogdict[row[0]]
        except: backlog = 0
        previtem = row[0]
      backlog += float(row[4]) - float(row[5])
      yield {
        'item': row[0],
        'bucket': row[1],
        'startdate': python_date(row[2]),
        'enddate': python_date(row[3]),
        'demand': round(row[4],1),
        'supply': round(row[5],1),
        'backlog': round(backlog,1),
        }


class DetailReport(GridReport):
  '''
  A list report to show delivery plans for demand.
  '''
  template = 'output/demandplan.html'
  title = _("Demand plan detail")
  basequeryset = Demand.objects.all()
  model = Demand
  frozenColumns = 0
  editable = False
  multiselect = False
  rows = (
    GridFieldText('demand', title=_('demand'), key=True, editable=False, formatter='demand'),
    GridFieldText('item', title=_('item'), formatter='item', editable=False),
    GridFieldText('customer', title=_('customer'), formatter='customer', editable=False),
    GridFieldNumber('quantity', title=_('quantity'), editable=False),
    GridFieldNumber('planquantity', title=_('planned quantity'), editable=False),
    GridFieldDateTime('due', title=_('due date'), editable=False),
    GridFieldDateTime('plandate', title=_('planned date'), editable=False),
    GridFieldInteger('operationplan', title=_('operationplan'), editable=False),
    )


@staff_member_required
def GraphData(request, entity):
  basequery = Item.objects.filter(pk__exact=entity)
  (bucket,start,end,bucketlist) = getBuckets(request)
  demand = []
  supply = []
  backlog = []
  for x in OverviewReport.query(request, basequery, bucket, start, end):
    demand.append(x['demand'])
    supply.append(x['supply'])
    backlog.append(x['backlog'])
  context = {
    'buckets': bucketlist,
    'demand': demand,
    'supply': supply,
    'backlog': backlog,
    'axis_nth': len(bucketlist) / 20 + 1,
    }
  return HttpResponse(
    loader.render_to_string("output/demand.xml", context, context_instance=RequestContext(request)),
    mimetype='application/xml; charset=%s' % settings.DEFAULT_CHARSET
    )
